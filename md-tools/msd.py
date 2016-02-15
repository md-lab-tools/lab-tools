#!/usr/bin/env python
"""
Copyright (C) 2015 Jakub Krajniak <jkrajniak@gmail.com>

This file is distributed under free software licence:
you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import argparse
import functools
from libs import bonds
from libs import files_io
import h5py
import multiprocessing as mp
import numpy


def _args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--chain_length', required=True, type=int)
    parser.add_argument('--number_of_chains', required=True, type=int)
    parser.add_argument('--begin', default=0, type=int)
    parser.add_argument('--end', default=-1, type=int)
    parser.add_argument('--max_tau', default=1000, type=int)
    parser.add_argument('--output')
    parser.add_argument('--output_prefix', default='')
    parser.add_argument('--csv', default=False, action='store_true')
    parser.add_argument('--group', default='atoms')
    parser.add_argument('--nt', default=1, type=int)
    parser.add_argument('--every-frame', default=1, type=int, dest='step')
    parser.add_argument('--restart-every', default=1, type=int, dest='r_every')
    parser.add_argument('--remove_sys_com', choices=('yes', 'no'), default='yes', dest='remove_com', 
                        help='Substracts movement of whole system')
    parser.add_argument('--with-com', choices=('yes', 'no'), default='yes', help='Calculates MSD for COM')
    parser.add_argument('--types', help='Valid types (comma separated list of particle types)', type=str)
    parser.add_argument('in_file')

    return parser.parse_args()


def calculate_com(chain_length, masses, chains, trj):
    tot_mass = numpy.sum(masses[:chain_length])
    return bonds.calculate_com_chains(trj, chain_length, chains, masses[:chain_length], tot_mass)


def clip_data(input, start, stop, step=1, raw=False):
    if start == 0 and stop == -1 and step == 1:
        d = input
    else:
        d = input[start:stop:step]
    if raw:
        return d
    else:
        return numpy.array(d)


if __name__ == '__main__':
    args = _args()
    data = h5py.File(args.in_file, 'r')
    
    number_of_chains = args.number_of_chains
    
    ids = None
    if 'id' in data['/particles/{}/'.format(args.group)].keys():
        print('Found id/ dataset, columns will be sorted.')
        ids = clip_data(data['/particles/{}/id/value'.format(args.group)], args.begin, args.end, args.step, raw=True)
    
    print('Trajectory shape: {}'.format(data['/particles/{}/position/value'.format(args.group)].shape))    

    trj = clip_data(data['/particles/{}/position/value'.format(args.group)], args.begin, args.end, args.step)
    if ids is not None:
        trj = files_io.sort_h5md_array(trj, ids)
    box = data['/particles/{}/box/edges'.format(args.group)]
    if 'value' in box:
        box = numpy.array(box['value'][0])
    else:
        box = numpy.array(box)
    
    if 'image' in data['/particles/{}'.format(args.group)].keys():
        print('Found image dataset, computing absolute trajectory...')
        image = numpy.array(data['/particles/{}/image/value'.format(args.group)][args.begin:args.end:args.step])
        if ids is not None:
            image = files_io.sort_h5md_array(image, ids)
        trj = trj + box*image

    # TODO: assumption that masses does not change during simulation.
    masses = data['/particles/{}/mass'.format(args.group)]
    if 'value' in masses:
        masses = masses['value']
        if ids is not None:
            masses = files_io.sort_h5md_array(masses, ids, 1)
        print('Warning!: only for time-independent masses')
        masses = masses[0]
    masses = numpy.array(masses)

    species = None
    valid_types = None
    if 'species' in data['/particles/{}'.format(args.group)]:
        species = data['/particles/{}/species'.format(args.group)]
        if 'value' in species:
            species = clip_data(species['value'], args.begin, args.end, args.step)
            if ids is not None:
                species = files_io.sort_h5md_array(species, ids)
        else:
            species = clip_data(species, args.begin, args.end, args.step)
        if args.types:
            typs = map(int, args.types.split(','))
            valid_types = species == typs[0]
            for t in typs[1:]:
                valid_types += species == t
            print('Calculating MSD only for particle types: {}'.format(typs))
    elif args.types is not None:
        raise RuntimeError('Species dataset not found, though --types defined')
    
    # Filter data againts types. 
    if valid_types is not None:
        print('Found valid types, filtering data...')
        old_shape = trj.shape
        masses = masses[valid_types[0]]
        trj = trj[valid_types].reshape(old_shape[0], args.chain_length*number_of_chains, 3)
        print('Old trj.shape={}, new_shape={}'.format(old_shape, trj.shape))

    dt = (data['/particles/{}/position/time'.format(args.group)][args.begin:args.end:args.step][2]
          - data['/particles/{}/position/time'.format(args.group)][args.begin:args.end:args.step][1])

    data.close()    

    max_tau = args.max_tau / dt    

    nt = args.nt
    args.begin = 0
    if args.end == -1:
        args.end = len(trj)
    num_frames = args.end

    print('Trajectory length {}'.format(len(trj)))
    print('Chain length {}'.format(args.chain_length))
    print('dt: {}'.format(dt))
    print('max_tau: {}'.format(max_tau*dt))
    
    if args.with_com == 'yes': 
        print('Calculating COM for chains with {} processors'.format(args.nt))
        func = functools.partial(calculate_com, args.chain_length, masses, number_of_chains)
        pool = mp.Pool(processes=args.nt)
        results = pool.map(func, trj)
        pool.close()
        pool.join()
        traj_com = numpy.array([x[0] for x in results])
        traj_sys_com = numpy.array([x[1] for x in results])
    else:
        traj_com = trj
        number_of_chains = args.number_of_chains * args.chain_length
        traj_sys_com = numpy.zeros(shape=(len(traj_com), 3), dtype=float)

    if args.remove_com == 'no':
        print 'Without remove movement of COM'
        traj_sys_com = numpy.zeros(shape=(len(traj_com), 3), dtype=float)

    print('Calculating MSD...')
    func = functools.partial(bonds.calculate_msd_single, traj_com, traj_sys_com, box, number_of_chains, args.r_every)
    pool = mp.Pool(processes=args.nt)
    # Each of process gets single tau, data are replicted among CPUs.
    input_data = numpy.arange(0.0, max_tau+1)
    results = pool.map(func, input_data)
    pool.close()
    pool.join()
    
    print('Collecting data...')
    msd, msd_error = numpy.zeros(max_tau+1), numpy.zeros(max_tau+1)
    for t in range(int(max_tau)+1):
        msd[t] = results[t][0]
        msd_error[t] = results[t][1]
    
    time_column = numpy.arange(0.0, len(msd)*dt, dt)

    output = '{}msd_{}'.format(args.output_prefix, ''.join(args.in_file.split('.')[0:-1]))
    if args.output:
        output = args.output
    if args.csv:
        output += '.csv'
        numpy.savetxt(output, numpy.column_stack((time_column, msd, msd_error)))
    else:
        numpy.save(output, numpy.column_stack((time_column, msd, msd_error)))
    print('Saving to {}...'.format(output))