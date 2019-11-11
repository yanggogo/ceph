"""
Ceph cluster task, deployed via ceph-daemon and ssh orchestrator
"""
from cStringIO import StringIO

import argparse
import configobj
import contextlib
import errno
import logging
import os
import json
import time
import gevent
import re
import socket
import uuid

from paramiko import SSHException
from ceph_manager import CephManager, write_conf
from tarfile import ReadError
from tasks.cephfs.filesystem import Filesystem
from teuthology import misc as teuthology
from teuthology import contextutil
from teuthology import exceptions
from teuthology.orchestra import run
import ceph_client as cclient
from teuthology.orchestra.daemon import DaemonGroup
from tasks.daemonwatchdog import DaemonWatchdog

# these items we use from ceph.py should probably eventually move elsewhere
from tasks.ceph import get_mons

CEPH_ROLE_TYPES = ['mon', 'mgr', 'osd', 'mds', 'rgw']

log = logging.getLogger(__name__)


def shell(ctx, remote, args, **kwargs):
    testdir = teuthology.get_testdir(ctx)
    return remote.run(
        args=[
            'sudo',
            '{}/ceph-daemon'.format(testdir),
            '--image', ctx.image,
            'shell',
            '-c', '{}/ceph.conf'.format(testdir),
            '-k', '{}/ceph.keyring'.format(testdir),
            '--fsid', ctx.fsid,
            '--',
            ] + args,
        **kwargs
    )

def build_initial_config(ctx, config):
    #path = os.path.join(os.path.dirname(__file__), 'ceph.conf.template')
    conf = configobj.ConfigObj() #path, file_error=True)

    conf.setdefault('global', {})
    conf['global']['fsid'] = ctx.fsid

    # overrides
    for section, keys in config['conf'].items():
        for key, value in keys.items():
            log.info("[%s] %s = %s" % (section, key, value))
            if section not in conf:
                conf[section] = {}
            conf[section][key] = value

    return conf

@contextlib.contextmanager
def normalize_hostnames(ctx):
    """
    Ensure we have short hostnames throughout, for consistency between
    remote.shortname and socket.gethostname() in ceph-daemon.
    """
    log.info('Normalizing hostnames...')
    ctx.cluster.run(args=[
        'sudo',
        'hostname',
        run.Raw('$(hostname -s)'),
    ])

    try:
        yield
    finally:
        pass

@contextlib.contextmanager
def download_ceph_daemon(ctx, config):
    log.info('Downloading ceph-daemon...')
    testdir = teuthology.get_testdir(ctx)
    branch = config.get('ceph-daemon-branch', 'master')

    ctx.cluster.run(
        args=[
            'curl', '--silent',
            'https://raw.githubusercontent.com/ceph/ceph/%s/src/ceph-daemon/ceph-daemon' % branch,
            run.Raw('>'),
            '{tdir}/ceph-daemon'.format(tdir=testdir),
            run.Raw('&&'),
            'test', '-s',
            '{tdir}/ceph-daemon'.format(tdir=testdir),
            run.Raw('&&'),
            'chmod', '+x',
            '{tdir}/ceph-daemon'.format(tdir=testdir),
        ],
    )

    try:
        yield
    finally:
        log.info('Removing cluster...')
        ctx.cluster.run(args=[
            'sudo',
            '{}/ceph-daemon'.format(testdir),
            'rm-cluster',
            '--fsid', ctx.fsid,
            '--force',
        ])

        log.info('Removing ceph-daemon ...')
        ctx.cluster.run(
            args=[
                'rm',
                '-rf',
                '{tdir}/ceph-daemon'.format(tdir=testdir),
            ],
        )

@contextlib.contextmanager
def ceph_log(ctx, config, fsid):
    try:
        yield

    finally:
        if ctx.archive is not None and \
                not (ctx.config.get('archive-on-error') and ctx.summary['success']):
            # and logs
            log.info('Compressing logs...')
            run.wait(
                ctx.cluster.run(
                    args=[
                        'sudo',
                        'find',
                        '/var/log/ceph/' + fsid,
                        '-name',
                        '*.log',
                        '-print0',
                        run.Raw('|'),
                        'sudo',
                        'xargs',
                        '-0',
                        '--no-run-if-empty',
                        '--',
                        'gzip',
                        '--',
                    ],
                    wait=False,
                ),
            )

            log.info('Archiving logs...')
            path = os.path.join(ctx.archive, 'remote')
            try:
                os.makedirs(path)
            except OSError as e:
                pass
            for remote in ctx.cluster.remotes.keys():
                sub = os.path.join(path, remote.name)
                try:
                    os.makedirs(sub)
                except OSError as e:
                    pass
                teuthology.pull_directory(remote, '/var/log/ceph/' + fsid,
                                          os.path.join(sub, 'log'))

@contextlib.contextmanager
def ceph_crash(ctx, fsid):
    """
    Gather crash dumps from /var/lib/ceph/$fsid/crash
    """
    try:
        yield

    finally:
        if ctx.archive is not None:
            log.info('Archiving crash dumps...')
            path = os.path.join(ctx.archive, 'remote')
            try:
                os.makedirs(path)
            except OSError as e:
                pass
            for remote in ctx.cluster.remotes.keys():
                sub = os.path.join(path, remote.name)
                try:
                    os.makedirs(sub)
                except OSError as e:
                    pass
                try:
                    teuthology.pull_directory(remote,
                                              '/var/lib/ceph/%s/crash' % fsid,
                                              os.path.join(sub, 'crash'))
                except ReadError as e:
                    pass

@contextlib.contextmanager
def ceph_bootstrap(ctx, config, fsid):
    testdir = teuthology.get_testdir(ctx)

    mons = ctx.mons
    first_mon = sorted(mons.keys())[0]
    (mon_remote,) = ctx.cluster.only(first_mon).remotes.keys()
    log.info('First mon is %s on %s' % (first_mon, mon_remote.shortname))
    ctx.first_mon = first_mon

    others = ctx.cluster.remotes[mon_remote]
    log.info('others %s' % others)
    mgrs = sorted([r for r in others if r.startswith('mgr.')])
    if not mgrs:
        raise RuntimeError('no mgrs on the same host as first mon %s' % first_mon)
    first_mgr = mgrs[0]
    log.info('First mgr is %s' % (first_mgr))
    ctx.first_mgr = first_mgr

    try:
        # write seed config
        log.info('Writing seed config...')
        conf_fp = StringIO()
        seed_config = build_initial_config(ctx, config)
        seed_config.write(conf_fp)
        teuthology.write_file(
            remote=mon_remote,
            path='{}/seed.ceph.conf'.format(testdir),
            data=conf_fp.getvalue())

        # bootstrap
        log.info('Bootstrapping...')
        cmd = [
            'sudo',
            '{}/ceph-daemon'.format(testdir),
            '--image', ctx.image,
            'bootstrap',
            '--fsid', fsid,
            '--mon-id', first_mon[4:],
            '--mgr-id', first_mgr[4:],
            '--config', '{}/seed.ceph.conf'.format(testdir),
            '--output-config', '{}/ceph.conf'.format(testdir),
            '--output-keyring', '{}/ceph.keyring'.format(testdir),
            '--output-pub-ssh-key', '{}/ceph.pub'.format(testdir),
        ]
        if mons[first_mon].startswith('['):
            cmd += ['--mon-addrv', mons[first_mon]]
        else:
            cmd += ['--mon-ip', mons[first_mon]]
        if config.get('skip_dashboard'):
            cmd += ['--skip-dashboard']
        # bootstrap makes the keyring root 0600, so +r it for our purposes
        cmd += [
            run.Raw('&&'),
            'sudo', 'chmod', '+r', '{}/ceph.keyring'.format(testdir),
        ]
        mon_remote.run(args=cmd)

        # fetch keys and configs
        log.info('Fetching config...')
        ctx.config_file = teuthology.get_file(
            remote=mon_remote,
            path='{}/ceph.conf'.format(testdir))
        log.info('Fetching client.admin keyring...')
        ctx.admin_keyring = teuthology.get_file(
            remote=mon_remote,
            path='{}/ceph.keyring'.format(testdir))
        log.info('Fetching mon keyring...')
        ctx.mon_keyring = teuthology.get_file(
            remote=mon_remote,
            path='/var/lib/ceph/%s/%s/keyring' % (fsid, first_mon),
            sudo=True)

        # fetch ssh key, distribute to additional nodes
        log.info('Fetching pub ssh key...')
        ssh_pub_key = teuthology.get_file(
            remote=mon_remote,
            path='{}/ceph.pub'.format(testdir)
        ).strip()

        log.info('Installing pub ssh key for root users...')
        ctx.cluster.run(args=[
            'sudo', 'install', '-d', '-m', '0700', '/root/.ssh',
            run.Raw('&&'),
            'echo', ssh_pub_key,
            run.Raw('|'),
            'sudo', 'tee', '-a', '/root/.ssh/authorized_keys',
            run.Raw('&&'),
            'sudo', 'chmod', '0600', '/root/.ssh/authorized_keys',
        ])

        # add other hosts
        for remote in ctx.cluster.remotes.keys():
            if remote == mon_remote:
                continue
            log.info('Writing conf and keyring to %s' % remote.shortname)
            teuthology.write_file(
                remote=remote,
                path='{}/ceph.conf'.format(testdir),
                data=ctx.config_file)
            teuthology.write_file(
                remote=remote,
                path='{}/ceph.keyring'.format(testdir),
                data=ctx.admin_keyring)

            log.info('Adding host %s to orchestrator...' % remote.shortname)
            shell(ctx, remote, [
                'ceph', 'orchestrator', 'host', 'add',
                remote.shortname
            ])

        yield

    finally:
        log.info('Cleaning up testdir ceph.* files...')
        ctx.cluster.run(args=[
            'rm', '-f',
            '{}/seed.ceph.conf'.format(testdir),
            '{}/ceph.pub'.format(testdir),
            '{}/ceph.conf'.format(testdir),
            '{}/ceph.keyring'.format(testdir),
        ])

        log.info('Stopping all daemons...')
        ctx.cluster.run(args=['sudo', 'systemctl', 'stop', 'ceph.target'])

@contextlib.contextmanager
def ceph_mons(ctx, config):
    """
    Deploy any additional mons
    """
    testdir = teuthology.get_testdir(ctx)
    num_mons = 1

    try:
        for remote, roles in ctx.cluster.remotes.items():
            for mon in [r for r in roles if r.startswith('mon.')]:
                if mon == ctx.first_mon:
                    continue
                log.info('Adding %s on %s' % (mon, remote.shortname))
                num_mons += 1
                shell(ctx, remote, [
                    'ceph', 'orchestrator', 'mon', 'update',
                    str(num_mons),
                    remote.shortname + ':' + ctx.mons[mon],
                ])

                while True:
                    log.info('Waiting for %d mons in monmap...' % (num_mons))
                    r = shell(
                        ctx=ctx,
                        remote=remote,
                        args=[
                            'ceph', 'mon', 'dump', '-f', 'json',
                        ],
                        stdout=StringIO(),
                    )
                    j = json.loads(r.stdout.getvalue())
                    if len(j['mons']) == num_mons:
                        break
                    time.sleep(1)

        ## FIXME: refresh ceph.conf files for all mons + first mgr ##

        yield

    finally:
        pass

@contextlib.contextmanager
def ceph_mgrs(ctx, config):
    """
    Deploy any additional mgrs
    """
    testdir = teuthology.get_testdir(ctx)
    (remote,) = ctx.cluster.only(ctx.first_mon).remotes.keys()

    try:
        nodes = []
        for remote, roles in ctx.cluster.remotes.items():
            for mgr in [r for r in roles if r.startswith('mgr.')]:
                if mgr == ctx.first_mgr:
                    continue
                log.info('Adding %s on %s' % (mgr, remote.shortname))
                ### FIXME: we don't get to choose the mgr names ####
                nodes.append(remote.shortname)
        shell(ctx, remote, [
            'ceph', 'orchestrator', 'mgr', 'update',
            str(len(nodes) + 1)] + nodes
        )

        yield

    finally:
        pass

@contextlib.contextmanager
def ceph_osds(ctx, config):
    """
    Deploy OSDs
    """
    try:
        log.info('Zapping devices...')
        devs_by_remote = {}
        for remote, roles in ctx.cluster.remotes.items():
            devs = teuthology.get_scratch_devices(remote)
            for dev in devs:
                shell(ctx, remote, [
                    'ceph-volume', 'lvm', 'zap', dev])
            devs_by_remote[remote] = devs

        log.info('Deploying OSDs...')
        for remote, roles in ctx.cluster.remotes.items():
            devs = devs_by_remote[remote]
            for osd in [r for r in roles if r.startswith('osd.')]:
                assert devs   ## FIXME ##
                dev = devs.pop()
                log.info('Deploying %s on %s with %s...' % (
                    osd, remote.shortname, dev))
                shell(ctx, remote, [
                    'ceph', 'orchestrator', 'osd', 'create',
                    remote.shortname + ':' + dev
                ])

        yield
    finally:
        pass

@contextlib.contextmanager
def ceph_initial():
    try:
        yield
    finally:
        log.info('Teardown complete')

## public methods
@contextlib.contextmanager
def stop(ctx, config):
    """
    Stop ceph daemons

    For example::
      tasks:
      - ceph.stop: [mds.*]

      tasks:
      - ceph.stop: [osd.0, osd.2]

      tasks:
      - ceph.stop:
          daemons: [osd.0, osd.2]

    """
    if config is None:
        config = {}
    elif isinstance(config, list):
        config = {'daemons': config}

    daemons = ctx.daemons.resolve_role_list(
        config.get('daemons', None), CEPH_ROLE_TYPES, True)
    clusters = set()

    for role in daemons:
        cluster, type_, id_ = teuthology.split_role(role)
        ctx.daemons.get_daemon(type_, id_, cluster).stop()
        clusters.add(cluster)

#    for cluster in clusters:
#        ctx.ceph[cluster].watchdog.stop()
#        ctx.ceph[cluster].watchdog.join()

    yield


@contextlib.contextmanager
def task(ctx, config):
    if config is None:
        config = {}

    assert isinstance(config, dict), \
        "task only supports a dictionary for configuration"

    overrides = ctx.config.get('overrides', {})
    teuthology.deep_merge(config, overrides.get('ceph', {}))
    log.info('Config: ' + str(config))

    testdir = teuthology.get_testdir(ctx)

    ## FIXME i don't understand multicluster ##
    first_ceph_cluster = False
    if not hasattr(ctx, 'daemons'):
        first_ceph_cluster = True
        ctx.daemons = DaemonGroup()

    if not hasattr(ctx, 'ceph'):
        ctx.ceph = {}

    ## FIXME i don't understand multicluster ##
    if 'cluster' not in config:
        config['cluster'] = 'ceph'
    cluster_name = config['cluster']
    ctx.ceph[cluster_name] = argparse.Namespace()

    #validate_config(ctx, config)

    # image
    branch = config.get('branch', 'master')
    ### FIXME ###
    if branch in ['master', 'nautilus']:
        ctx.image = 'ceph/daemon-base:latest-%s-devel' % branch
    else:
#        ctx.image = 'ceph-ci/ceph:%s' % branch
        ctx.image = 'cephci/daemon-base:%s' % branch
    log.info('Cluster image is %s' % ctx.image)

    # uuid
    fsid = str(uuid.uuid1())
    ctx.fsid = fsid
    log.info('Cluster fsid is %s' % fsid)
    ## FIXME i don't understand multicluster ##
    ctx.ceph[cluster_name].fsid = fsid

    # mon ips
    log.info('Choosing monitor IPs and ports...')
    remotes_and_roles = ctx.cluster.remotes.items()
    roles = [role_list for (remote, role_list) in remotes_and_roles]
    ips = [host for (host, port) in
           (remote.ssh.get_transport().getpeername() for (remote, role_list) in remotes_and_roles)]
    ctx.mons = get_mons(
        roles, ips, cluster_name,
        mon_bind_msgr2=config.get('mon_bind_msgr2', True),
        mon_bind_addrvec=config.get('mon_bind_addrvec', True),
        )
    log.info('Monitor IPs: %s' % ctx.mons)

    with contextutil.nested(
            lambda: ceph_initial(),
            lambda: normalize_hostnames(ctx=ctx),
            lambda: download_ceph_daemon(ctx=ctx, config=config),
            lambda: ceph_log(ctx=ctx, config=config, fsid=fsid),
            lambda: ceph_crash(ctx=ctx, fsid=fsid),
            lambda: ceph_bootstrap(ctx=ctx, config=config, fsid=fsid),
            lambda: ceph_mons(ctx=ctx, config=config),
            lambda: ceph_mgrs(ctx=ctx, config=config),
            lambda: ceph_osds(ctx=ctx, config=config),
    ):
        try:
            log.info('Setup complete, yielding')
            yield

        finally:
            log.info('Teardown begin')

