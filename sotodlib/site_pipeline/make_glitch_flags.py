from argparse import ArgumentParser
import logging
import numpy as np
import os
import sys
import yaml

import sotodlib
from sotodlib import core, flags, site_pipeline

logger = logging.getLogger(__name__)

def get_parser():
    parser = ArgumentParser()
    parser.add_argument('obs_id',help=
                        "Observation for which to generate flags.")
    parser.add_argument('-c', '--config-file', help=
                        "Configuration file.")
    parser.add_argument('-v', '--verbose', action='count',
                        default=0, help="Pass multiple times to increase.")
    return parser

def get_config(args):
    cfg = yaml.safe_load(open(args.config_file, 'r'))
    for k in ['obs_id', 'verbose']:
        cfg[k] = getattr(args, k)
    return cfg

def main(args=None):
    if args is None:
        args = sys.argv[1:]
    parser = get_parser()
    config = get_config(parser.parse_args(args))

    if config['verbose'] >= 1:
        logger.setLevel('INFO')
    if config['verbose'] >= 2:
        sotodlib.logger.setLevel('INFO')
    if config['verbose'] >= 3:
        sotodlib.logger.setLevel('DEBUG')

    ctx = core.Context(config['context_file'])

    group_by = config['subobs'].get('use', 'detset')
    if group_by == 'detset':
        groups = ctx.obsfiledb.get_detsets(config['obs_id'])
    elif group_by.startswith('dets:'):
        group_by = group_by.split(':',1)[1]
        groups = ctx.detdb.props(props=group_by).distinct()[group_by]
    else:
        raise ValueError("Can't group by '{group_by}'")

    if os.path.exists(config['archive']['index']):
        logger.info(f'Mapping {config["archive"]["index"]} for the archive index.')
        db = core.metadata.ManifestDb(config['archive']['index'])
    else:
        logger.info(f'Creating {config["archive"]["index"]} for the archive index.')
        scheme = core.metadata.ManifestScheme()
        scheme.add_exact_match('obs:obs_id')
        if group_by != 'detset':
            scheme.add_exact_match('dets:' + group_by)
        scheme.add_data_field('dataset')
        db = core.metadata.ManifestDb(config['archive']['index'], scheme=scheme)

    for group in groups:
        logger.info(f'Loading {config["obs_id"]}:{group_by}={group}')
        if group_by == 'detset':
            # Load pointing and dets axis; we do
            tod = ctx.get_obs(config['obs_id'], detsets=[group])
        else:
            tod = ctx.get_obs(config['obs_id'], dets={group_by: group})

        # Load / compute mask parameters.
        flag_params = config['flag_params']['default']

        tod_flags, csr = flags.get_glitch_flags(tod, full_output=True, 
                                            **flag_params)

        # Compute fraction of samples
        weight = np.mean(tod_flags.get_stats()['samples']) / tod_flags.shape[1]
        logger.info(f'Total mask weight is {weight*100:.2}%.')

        # Wrap result into AxisManager for HDF5 off-load.
        aman = core.AxisManager(tod.dets)
        aman.wrap('glitch_flags', tod_flags, [(0, 'dets')])
        aman.wrap('glitch_detections', csr, [(0, 'dets'), (1,'samps')])

        # Get file + dataset from policy.
        policy = site_pipeline.util.ArchivePolicy.from_params(config['archive']['policy'])
        dest_file, dest_dataset = policy.get_dest(config['obs_id'])
        if group_by == 'detset':
            dest_dataset += '_' + group
        aman.save(dest_file, dest_dataset, overwrite=True)

        # Update the index.
        db_data = {'obs:obs_id': config['obs_id'],
                   'dataset': dest_dataset}
        if group_by != 'detset':
            db_data['dets:'+group_by] = group
        db.add_entry(db_data, dest_file)

    # Return something?
    return tod, aman