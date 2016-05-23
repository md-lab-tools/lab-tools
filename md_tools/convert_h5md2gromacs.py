#!/usr/bin/env python
"""
Copyright (C) 2016 Jakub Krajniak <jkrajniak@gmail.com>

This file is part of lab-tools.

lab-tools is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
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
import collections
import warnings
import xml.etree.ElementTree as etree
from multiprocessing import Pool
import functools
import sys

import h5py
import networkx
import numpy as np

from md_libs import files_io

__doc__ = 'Convert H5MD to GROMACS Topology'


ValidTypes = collections.namedtuple('ValidTypes', ['bonds', 'angles', 'dihedrals', 'pairs'])


def _args():
    parser = argparse.ArgumentParser(description='Convert H5MD to GROMACS topology', add_help=True)
    parser.add_argument('--h5', help='Input H5MD file', required=True, dest='h5')
    parser.add_argument('--itp', help='Input ITP file', required=True)
    parser.add_argument('--options', help='Options file', required=True)
    parser.add_argument('--timeframe', help='Which time frame', default=-1, type=int)
    parser.add_argument('--out', help='GROMACS out file', required=True)
    parser.add_argument('--out_coordinate', help='.gro file', required=True)

    return parser


def read_settings(input_file):
    """Reads the settings XML settings file and create local representation."""
    tree = etree.parse(input_file)
    root = tree.getroot()
    type2chain = {}
    name2type = {}
    output_type = collections.namedtuple('output_type', ['chain_name', 'type_name'])
    for e in root.find('type2chain').text.split():
        type_id, chain_name, type_name = e.split(':')
        type2chain[int(type_id)] = output_type(chain_name.strip(), type_name.strip())
        if type_name.strip() in name2type:
            raise RuntimeError('Type with name {} already found, wrong type2chain section'.format(type_name))
        name2type[type_name.strip()] = int(type_id)
    # Getes molecule properties
    molecule_properties = {}
    out_prop = collections.namedtuple('MoleculeProperties', ['name', 'size', 'nrexcl', 'nrmols'])
    for e in root.findall('molecule_type'):
        name = e.attrib['name']
        molecule_properties[name] = out_prop(
            name, int(e.attrib['size']), int(e.attrib['nrexcl']), int(e.attrib['nrmols']))

    # Name sequence depends on the type sequence.
    name_sequence = collections.defaultdict(dict)
    output_seq = collections.namedtuple('OutputSeq', ['atom_names', 'res_name'])
    for e in root.findall('name_seq'):
        type_seq = tuple(map(int, e.attrib['seq'].split()))
        if len(type_seq) != molecule_properties[e.attrib['chain_name']].size:
            raise RuntimeError("Molecule {} name_seq length ({}) is different from declared size {}.".format(
                e.attrib['chain_name'], len(type_seq), molecule_properties[e.attrib['chain_name']].size))
        if type_seq in name_sequence[e.attrib['chain_name']]:
            raise RuntimeError(
                'Type sequence {} already defined for chain_name {}'.format(type_seq, e.attrib['chain_name']))
        name_sequence[e.attrib['chain_name']][type_seq] = output_seq(e.text.split(), e.attrib['res_name'].strip())

    # Properties of h5md file.
    h5md_file = root.find('h5md')
    h5md_properties = collections.namedtuple(
        'H5MDproperties', ['group', 'connection_groups'])(
            h5md_file.attrib['atom_groups'], map(str.strip, h5md_file.attrib['connection_groups'].split(',')))

    output_tuple = collections.namedtuple(
        'Output', ['name_seq', 'type2chain', 'name2type', 'molecule_properties', 'h5md_file'])
    return output_tuple(
        name_sequence,
        type2chain,
        name2type,
        molecule_properties,
        h5md_properties)


def _generate_bonded_terms(g, valid_bonded_types, input_data):
    """Multiprocess generate of bonded terms."""
    # Generate angles
    angles = set([])
    dihedrals = set([])
    pairs = set()
    nodes = g.nodes()
    idx, i = input_data
    for j in nodes[idx + 1:]:
        # Generate angles
        path_i_j = networkx.all_simple_paths(g, i, j, 4)
        for x in path_i_j:
            if len(x) == 3:
                type_an = tuple(g.node[z]['type_id'] for z in x)
                r_type_an = tuple(reversed(type_an))
                param = valid_bonded_types.angles.get(type_an, valid_bonded_types.angles.get(r_type_an))
                if param:
                    angles.add(tuple(x + [param]))
            elif len(x) == 4:
                type_an = tuple(g.node[z]['type_id'] for z in x)
                r_type_an = tuple(reversed(type_an))
                param = valid_bonded_types.dihedrals.get(type_an)
                if not param:
                    param = valid_bonded_types.dihedrals.get(r_type_an)
                if param:
                    dihedrals.add(tuple(x + [param]))
                # Check also pairs.
                p_type_an = (type_an[0], type_an[3])
                r_ptype_an = (type_an[3], type_an[0])
                param  = valid_bonded_types.pairs.get(p_type_an, valid_bonded_types.pairs.get(r_ptype_an))
                if param:
                    z = tuple([x[0], x[3], param])
                    pairs.add(z)

    return angles, dihedrals, pairs


def generate_bonded_terms(g, valid_bonded_types):
    """Generate bonded terms based on the types defined in itp file, from the graph structure."""
    bonds = set()

    for x1, x2 in g.edges():
        n1 = g.node[x1]
        n2 = g.node[x2]
        type_an = (n1['type_id'], n2['type_id'])
        r_type_an = tuple(reversed(type_an))
        param = valid_bonded_types.bonds.get(type_an, valid_bonded_types.bonds.get(r_type_an))
        if param:
            bonds.add((x1, x2, param))
        else:
            raise RuntimeError('Parameters for bond {}-{} not found'.format(x1, x2))

    angles = set([])
    dihedrals = set([])
    pairs = set()

    f = functools.partial(_generate_bonded_terms, g, valid_bonded_types)
    # Run on multiple CPUs.
    print('Generate bonded_terms on multi CPUs, it will take a while....')
    p = Pool()
    input_data = [(idx, i) for idx, i in enumerate(g.nodes())]
    num_tasks = float(len(input_data))
    print(num_tasks)
    out_map = p.imap(f, input_data, chunksize=100)
    for i, (a, d, p) in enumerate(out_map):
        sys.stdout.write('done {0:%}\r'.format(i / num_tasks))
        angles.update(a)
        dihedrals.update(d)
        pairs.update(p)

    return bonds, angles, dihedrals, pairs


def prepare_gromacs_topology(g, settings, itp_file, args):
    """Prepares GROMASC topology file. Not everything is supported!"""

    output_file = args.out

    output = files_io.GROMACSTopologyFile(output_file)
    output.init()

    output.header_section.append('; GROMACS like topology file\n')
    output.header_section.append('; parameters:\n')
    output.header_section.append(';    itp_file: {}\n'.format(args.itp))
    output.header_section.append(';    options_file: {}\n'.format(args.options))
    output.header_section.append(';    timeframe: {}\n\n'.format(args.timeframe))
    output.header_section.append('#include "./{}"\n'.format(itp_file.file_name))

    # Write defaults
    output.defaults = {
        'nbfunc': 1,
        'comb-rule': 1,
        'gen-pairs': 'no',
        'fudgeLJ': 0.0,
        'fudgeQQ': 0.0
    }
    warnings.warn('Warning, [ defaults ] section is set to {}'.format(output.defaults))

    for at_id, at_data in g.node.items():
        output.atoms[at_id] = files_io.TopoAtom(
            atom_id=at_id,
            atom_type=at_data['type_name'],
            chain_idx=at_data['chain_idx'],
            chain_name=at_data['res_name'],
            name=at_data['name'],
            cgnr=at_id,
            charge=at_data.get('charge', 0.0),
            mass=at_data['mass']
        )

    valid_bond_types = {}
    bond_type_params = {}
    btypeid = 1
    for i in itp_file.bondtypes:
        for j in itp_file.bondtypes[i]:
            bond_type_params[btypeid] = itp_file.bondtypes[i][j]
            valid_bond_types[tuple(map(settings.name2type.get, (i, j)))] = btypeid
            valid_bond_types[tuple(map(settings.name2type.get, (j, i)))] = btypeid
            btypeid += 1
    angle_type_params = {}
    atypeid = 1
    valid_angle_types = {}
    for i in itp_file.angletypes:
        for j in itp_file.angletypes[i]:
            for k in itp_file.angletypes[i][j]:
                angle_type_params[atypeid] = itp_file.angletypes[i][j][k]
                valid_angle_types[tuple(map(settings.name2type.get, (i, j, k)))] = atypeid
                valid_angle_types[tuple(map(settings.name2type.get, (k, j, i)))] = atypeid
                atypeid += 1
    dihedral_type_params = {}
    dtypeid = 1
    valid_dihedral_types = {}
    for i in itp_file.dihedraltypes:
        for j in itp_file.dihedraltypes[i]:
            for k in itp_file.dihedraltypes[i][j]:
                for l in itp_file.dihedraltypes[i][j][k]:
                    dihedral_type_params[dtypeid] = itp_file.dihedraltypes[i][j][k][l]
                    valid_dihedral_types[tuple(map(settings.name2type.get, (i, j, k, l)))] = dtypeid
                    valid_dihedral_types[tuple(map(settings.name2type.get, (l, k, j, i)))] = dtypeid
                    dtypeid += 1

    pair_type_params = {}
    ptypeid = 1
    valid_pair_types = {}
    for i in itp_file.pairtypes:
        for j in itp_file.pairtypes[i]:
            pair_type_params[ptypeid] = itp_file.pairtypes[i][j]
            valid_pair_types[tuple(map(settings.name2type.get, (i, j)))] = ptypeid
            valid_pair_types[tuple(map(settings.name2type.get, (j, i)))] = ptypeid
            ptypeid += 1

    bonds, angles, dihedrals, pairs = generate_bonded_terms(
        g, ValidTypes(valid_bond_types, valid_angle_types, valid_dihedral_types, {}))

    output.bonds = {
        tuple(x[:2]): [bond_type_params[x[2]]['func']] + bond_type_params[x[2]]['params']
        for x in bonds}
    output.angles = {
        tuple(x[:3]): [angle_type_params[x[3]]['func']] + angle_type_params[x[3]]['params']
        for x in angles}
    output.dihedrals = {
        tuple(x[:4]): [dihedral_type_params[x[4]]['func']] + dihedral_type_params[x[4]]['params']
        for x in dihedrals}
    output.pairs = {
        tuple(x[: 2]): [pair_type_params[x[2]]['func']] + pair_type_params[x[2]]['params']
        for x in pairs}

    for mol_name, mol_prop in settings.molecule_properties.items():
        output.moleculetype.append({
            'name': mol_name,
            'nrexcl': mol_prop.nrexcl
        })
        output.molecules.append({
            'name': mol_name,
            'mol': mol_prop.nrmols
        })
    output.system_name = settings.molecule_properties.keys()[0]

    output.write(output_file)

    return output


def prepare_coordinate(file_name, graph):
    """Prepare .gro file based on the graph structure with positions from given frame."""
    out_coordinate = files_io.GROFile(file_name)
    out_coordinate.box = graph.graph['box']
    for at_id, at_data in graph.node.items():
        out_coordinate.atoms[at_id] = files_io.Atom(
            atom_id=at_id,
            name=at_data['name'],
            chain_name=at_data['res_name'],
            chain_idx=at_data['chain_idx'],
            position=at_data['position']
        )
    out_coordinate.write(force=True)


def build_graph(h5, settings, timeframe):
    """Create Graph structure based on the connectivity."""
    g = networkx.Graph()
    # Create box.
    box = h5['/particles/{}/box/edges'.format(settings.h5md_file.group)]
    if 'value' in box:
        box = box['value'][timeframe]
    g.graph['box'] = np.array(box)
    # Create bond list.
    bond_list = []
    for group_name in settings.h5md_file.connection_groups:
        group_path = '/connectivity/{}/'.format(group_name)
        cl = h5[group_path]
        if 'value' in h5[group_path]:
            cl = cl['value'][timeframe]
        bond_list.extend([x for x in cl if -1 not in x])
    g.add_edges_from(bond_list)
    # Get types and generate the names of atoms.
    type_list = np.array([
        x for x in h5['/particles/{}/species/value'.format(settings.h5md_file.group)][timeframe] if x != -1
        ])
    positions = np.array([
        x for x in h5['/particles/{}/position/value'.format(settings.h5md_file.group)][timeframe]
    ])
    mass = np.array([
        x for x in h5['/particles/{}/mass/value'.format(settings.h5md_file.group)][timeframe]
    ])
    ids = np.array([
        x for x in h5['/particles/{}/id/value'.format(settings.h5md_file.group)][timeframe] if x != -1
    ])
    for i, pid in enumerate(ids):
        at_type = type_list[i]
        g.node[pid]['type_id'] = at_type
        type_name = settings.type2chain[at_type]
        g.node[pid]['type_name'] = type_name.type_name
        g.node[pid]['chain_name'] = type_name.chain_name
        g.node[pid]['position'] = positions[i]
        g.node[pid]['mass'] = mass[i]
    # Assign node name based on the sequence in given molecule.
    total_size = len(ids)
    pidx = 0
    chain_idx = collections.defaultdict(int)
    while total_size > 0:
        pid = ids[pidx]
        node = g.node[pid]
        mol_size = settings.molecule_properties[node['chain_name']].size
        # Chunk of types to match with appropriate sequence
        type_chunk = tuple(type_list[pidx:pidx+mol_size])
        name_seq = settings.name_seq[node['chain_name']][type_chunk]
        chain_idx[node['chain_name']] += 1
        for ni, i in enumerate(range(pidx, pidx+mol_size)):
            g.node[ids[i]]['name'] = name_seq.atom_names[ni]
            g.node[ids[i]]['res_name'] = name_seq.res_name
            g.node[ids[i]]['chain_idx'] = chain_idx[node['chain_name']]
        total_size -= mol_size
        pidx += mol_size

    return g


def main():
    args = _args().parse_args()

    h5 = h5py.File(args.h5, 'r')
    settings = read_settings(args.options)
    structure_graph = build_graph(h5, settings, args.timeframe)

    itp_file = files_io.GROMACSTopologyFile(args.itp)
    itp_file.read()

    prepare_coordinate(args.out_coordinate, structure_graph)
    prepare_gromacs_topology(structure_graph, settings, itp_file, args)


if __name__ == '__main__':
    main()