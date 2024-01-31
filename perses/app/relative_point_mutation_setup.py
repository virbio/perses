from __future__ import absolute_import

from perses.annihilation.relative import HybridTopologyFactory, RepartitionedHybridTopologyFactory, RESTCapableHybridTopologyFactory
from perses.rjmc.topology_proposal import PointMutationEngine
from perses.rjmc.geometry import FFAllAngleGeometryEngine

import simtk.openmm as openmm
import simtk.openmm.app as app
import simtk.unit as unit

from openmoltools import forcefield_generators
from openmmtools.constants import kB
from openff.toolkit.topology import Molecule
from openmmforcefields.generators import SystemGenerator

import numpy as np
import mdtraj as md
import copy
import pkg_resources

ENERGY_THRESHOLD = 1e-2
temperature = 300 * unit.kelvin
kT = kB * temperature
beta = 1.0/kT
ring_amino_acids = ['TYR', 'PHE', 'TRP', 'PRO', 'HIS', 'HID', 'HIE', 'HIP']
KNOWN_BOX_SHAPES = ['cube', 'octahedron', 'dodecahedron']

# Set up logger
import logging
_logger = logging.getLogger("setup")
_logger.setLevel(logging.INFO)

class PointMutationExecutor(object):
    """
    This class generates the input files necessary to run a relative binding free energy calculation for a protein mutation
    in a protein-ligand or protein-protein system. This class will generate two hybrid factories:
        1. 'wildtype' -> 'point mutant' complex hybrid factory
        2. 'wildtype' -> 'point mutant' apo hybrid factory (i.e. without ligand or protein partner bound)
    You can also choose to generate a single hybrid factory for apo (without a ligand or protein binding partner) -- just
    leave the `ligand_input` argument as None.

    The following factories are available (implemented in perses/annihilation/relative.py):
    - `HybridTopologyFactory` -- allows for alchemical scaling only
    - `RepartitionedHybridTopologyFactory` -- allows for REST scaling only
    - `RESTCapableHybridTopologyFactory` -- allows for alchemical and REST scaling at the same time and uses a softcore potential
    that lifts alchemical atoms into the 4th dimension.

    Example (for a protein:ligand system, create `HybridTopologyFactory`s and run parallel tempering on both complex and apo phases):
        from pkg_resources import resource_filename
        import openmm
        from openmm import unit, app
        from perses.app.relative_point_mutation_setup import PointMutationExecutor

        protein_path = 'data/perses_jacs_systems/thrombin/Thrombin_protein.pdb'
        ligands_path = 'data/perses_jacs_systems/thrombin/Thrombin_ligands.sdf'
        protein_filename = resource_filename('openmmforcefields', protein_path)
        ligand_input = resource_filename('openmmforcefields', ligands_path)

        pm_delivery = PointMutationExecutor(protein_filename=protein_filename,
                                    mutation_chain_id='2',
                                    mutation_residue_id='198',
                                     proposed_residue='THR',
                                     conduct_endstate_validation=False,
                                     ligand_input=ligand_input,
                                     ligand_index=0,
                                     forcefield_files=['amber14/protein.ff14SB.xml', 'amber14/tip3p.xml'],
                                     barostat=openmm.MonteCarloBarostat(1.0 * unit.atmosphere, 300 * unit.kelvin, 50),
                                     forcefield_kwargs={'removeCMMotion': False, 'constraints' : app.HBonds, 'hydrogenMass' : 3 * unit.amus},
                                     periodic_forcefield_kwargs={'ewaldErrorTolerance': 1e-4, 'nonbondedMethod': app.PME},
                                     small_molecule_forcefields='gaff-2.11')

        complex_htf = pm_delivery.get_complex_htf()
        apo_htf = pm_delivery.get_apo_htf()

        # Now we can build the hybrid repex samplers
        from perses.annihilation.lambda_protocol import LambdaProtocol
        from openmmtools.multistate import MultiStateReporter
        from perses.samplers.multistate import HybridRepexSampler
        from openmmtools import mcmc, cache, utils
        from perses.dispersed.utils import configure_platform

        suffix = 'run'; selection = 'not water'; checkpoint_interval = 10; n_states = 11; n_cycles = 5000

        for htf in [complex_htf, apo_htf]:
            lambda_protocol = LambdaProtocol(functions='default')
            reporter_file = 'reporter.nc'
            reporter = MultiStateReporter(reporter_file, analysis_particle_indices = htf.hybrid_topology.select(selection), checkpoint_interval = checkpoint_interval)
            hss = HybridRepexSampler(mcmc_moves=mcmc.LangevinSplittingDynamicsMove(timestep= 4.0 * unit.femtoseconds,
                                                                                  collision_rate=5.0 / unit.picosecond,
                                                                                  n_steps=250,
                                                                                  reassign_velocities=False,
                                                                                  n_restart_attempts=20,
                                                                                  splitting="V R R R O R R R V",
                                                                                  constraint_tolerance=1e-06),
                                                                                  hybrid_factory=htf,
                                                                                  online_analysis_interval=10)
            hss.setup(n_states=n_states, temperature=300*unit.kelvin, storage_file=reporter, lambda_protocol=lambda_protocol, endstates=False)

            platform = configure_platform(utils.get_fastest_platform().getName())
            hss.energy_context_cache = cache.ContextCache(capacity=None, time_to_live=None, platform=platform)
            hss.sampler_context_cache = cache.ContextCache(capacity=None, time_to_live=None, platform=platform)

            hss.extend(n_cycles)

    """
    def __init__(self,
                 # Topology generation parameters
                 protein_filename,
                 mutation_chain_id,
                 mutation_residue_id,
                 proposed_residue,
                 old_residue=None,
                 ligand_input=None,
                 ligand_index=0,
                 allow_undefined_stereo_sdf=False,

                 # Atom mapping parameters
                 extra_sidechain_map=None,
                 demap_CBs=False,

                 # Solvation parameters
                 is_vacuum=False,
                 is_solvated=False,
                 water_model='tip3p',
                 ionic_strength=0.15 * unit.molar,
                 padding=1.1 * unit.nanometer,
                 box_shape='cube',
                 transform_waters_into_ions_for_charge_changes=True,

                 # System generation parameters
                 forcefield_files=['amber14/protein.ff14SB.xml', 'amber14/tip3p.xml'],
                 barostat=openmm.MonteCarloBarostat(1.0 * unit.atmosphere, temperature, 50),
                 forcefield_kwargs={'removeCMMotion': False, 'constraints' : app.HBonds, 'hydrogenMass' : 3 * unit.amus},
                 periodic_forcefield_kwargs={'nonbondedMethod': app.PME, 'ewaldErrorTolerance': 0.00025},
                 nonperiodic_forcefield_kwargs=None,
                 small_molecule_forcefields='gaff-2.11',

                 # Hybrid factory parameters
                 conduct_endstate_validation=True,
                 flatten_torsions=False,
                 flatten_exceptions=False,
                 rest_radius=0.3 * unit.nanometer,
                 w_lifting=0.3 * unit.nanometer,
                 generate_unmodified_hybrid_topology_factory=True,
                 generate_repartitioned_hybrid_topology_factory=False,
                 generate_rest_capable_hybrid_topology_factory=False,
                 **kwargs):
        """
        arguments
            protein_filename : str
                path to protein (to mutate); .pdb, .cif
                Note: if there are nonstandard residues, the PDB should contain the standard residue name but the atoms/positions
                should correspond to the nonstandard residue. E.g. if I want to include HID, the PDB should contain HIS for the residue name,
                but the atoms should correspond to the atoms present in HID. You can use openmm.app.Modeller.addHydrogens() to
                generate a PDB like this. The same is true for the ligand_input, if its a PDB.
                Note: this can be the protein solute only or the solvated protein. if its the former, is_solvated should be set to False.
                if its the latter, is_solvated should be set to True.
            mutation_chain_id : str
                name of the chain to be mutated
            mutation_residue_id : str
                residue id to change
            proposed_residue : str
                three letter code of the residue to mutate to
            old_residue : str, default None
                name of the old residue, if is a nonstandard amino acid (e.g. LYN, ASH, HID, etc)
                if not specified, the old residue name will be inferred from the input PDB.
            ligand_input : str, default None
                path to ligand of interest (i.e. small molecule or protein)
                Note: if this is not solvated, it should be the ligand alone (.sdf or .pdb or .cif) and is_solvated should be set to False.
                if this is solvated, this should be the protein-ligand complex (.pdb or .cif) -- with the protein to be mutated first and
                the ligand second in the file -- and is_solvated should be set to True.
            ligand_index : int, default 0
                which ligand to use
            allow_undefined_stereo_sdf : bool, default False
                whether to allow an SDF file to contain undefined stereocenters
            extra_sidechain_map : dict, key: int, value: int, default None
                map of new to old sidechain atom indices to add to the default map (by default, we only map backbone atoms and CBs)
            demap_CBs : bool, default False
                whether to remove CBs from the mapping
            is_vacuum : bool, default False
                if False, then the protein (and complex, if ligand_input is specified) topology will be solvated and
                counterions will be added (if the transformation involves a charge change)
                otherwise, the topology will not be solvated and counterions will not be added
            is_solvated : bool, default False
                whether the protein (and complex, if ligand_input is specified) topology is already solvated.
                if False, the protein/complex topology is not already solvated
                otherwise, the input protein_filename (and ligand_input, if specified) are already solvated
                and should correspond to the solvated protein PDB and solvated complex PDB, respectively.
                if is_vacuum is True, this argument must be False
            water_model : str, default 'tip3p'
                solvent model to use for solvation
            ionic_strength : float * unit.molar, default 0.15 * unit.molar
                the total concentration of ions (both positive and negative) to add using Modeller.
                This does not include ions that are added to neutralize the system.
                Note that only monovalent ions are currently supported.
            padding : float * unit.nanometer, default 1.1 * unit.nanometer
                padding (in nanometers) to use for creating the solvent box
            box_shape : string, default 'cube'
                shape to use for creating the solvent box. options: 'cube', 'octahedron', 'dodecahedron'
            transform_waters_into_ions_for_charge_changes : bool, default True
                whether to introduce a counterion by transforming water(s) into ion(s) for charge changing transformations
                if False, counterions will not be introduced.
            forcefield_files : list of str, default ['amber14/protein.ff14SB.xml', 'amber14/tip3p.xml']
                forcefield files for proteins and solvent
            barostat : openmm.MonteCarloBarostat, default openmm.MonteCarloBarostat(1.0 * unit.atmosphere, 300 * unit.kelvin, 50)
                barostat to use
            forcefield_kwargs : dict, default {'removeCMMotion': False, 'constraints' : app.HBonds, 'hydrogenMass' : 3 * unit.amus}
                forcefield kwargs for system parametrization
            periodic_forcefield_kwargs : dict, default {'nonbondedMethod': app.PME, 'ewaldErrorTolerance': 1e-4}
                periodic forcefield kwargs for system parametrization
            nonperiodic_forcefield_kwargs : dict, default None
                non-periodic forcefield kwargs for system parametrization
            small_molecule_forcefields : str, default 'gaff-2.11'
                the forcefield string for small molecule parametrization
            conduct_endstate_validation : bool, default True
                whether to conduct an endstate validation of the hybrid factory. If using flatten_torsion=True and/or
                flatten_exceptions=True, endstate validation should not be conducted (otherwise, it will fail).
            flatten_torsions : bool, default False
                in the HybridTopologyFactory, flatten torsions involving unique new atoms at lambda = 0 and unique old atoms are lambda = 1
            flatten_exceptions : bool, default False
                in the HybridTopologyFactory, flatten exceptions involving unique new atoms at lambda = 0 and unique old atoms at lambda = 1
            rest_radius : unit.nanometer, default 0.3 * unit.nanometer
                radius for rest region, in nanometers
            w_lifting : unit.nanometer, default 0.3 * unit.nanometer
                maximal distance for lifting term, in nanometers
            generate_unmodified_hybrid_topology_factory : bool, default True
                whether to generate a vanilla HybridTopologyFactory
            generate_repartitioned_hybrid_topology_factory : bool, default False
                whether to generate a RepartitionedHybridTopologyFactory
            generate_rest_capable_hybrid_topology_factory : bool, default False
                whether to generate a RESTCapableHybridTopologyFactory
        TODO : allow argument for spectator ligands besides the 'ligand_file'

        """
        
        # Check arguments
        if not box_shape in KNOWN_BOX_SHAPES:
            raise ValueError(f"box_shape '{box_shape}' unsupported, must be one of {KNOWN_BOX_SHAPES}")
        if is_vacuum:
            assert not is_solvated, "is_vacuum is True, so is_solvated must be False, but you specified is_solvated to be True"

        # First thing to do is load the apo protein to mutate...
        if protein_filename.endswith('pdb'):
            protein_pdb = app.PDBFile(protein_filename)
        elif protein_filename.endswith('cif'):
            protein_pdb = app.PDBxFile(protein_filename)
        else:
            raise Exception("protein_filename file format is not supported. supported formats: .pdb, .cif")
        protein_positions, protein_topology, protein_md_topology = protein_pdb.positions, protein_pdb.topology, md.Topology.from_openmm(protein_pdb.topology)
        protein_topology = protein_topology if is_solvated else  protein_md_topology.to_openmm()
        protein_n_atoms = protein_md_topology.n_atoms

        # Load the ligand, if present
        molecules = []
        if ligand_input:
            if isinstance(ligand_input, str):
                if ligand_input.endswith('pdb'): # protein
                    ligand_pdb = app.PDBFile(ligand_input)
                    ligand_positions, ligand_topology, ligand_md_topology = ligand_pdb.positions, ligand_pdb.topology, md.Topology.from_openmm(ligand_pdb.topology)
                    ligand_n_atoms = ligand_md_topology.n_atoms

                elif ligand_input.endswith('cif'): # protein
                    ligand_pdb = app.PDBxFile(ligand_input)
                    ligand_positions, ligand_topology, ligand_md_topology = ligand_pdb.positions, ligand_pdb.topology, md.Topology.from_openmm(ligand_pdb.topology)
                    ligand_n_atoms = ligand_md_topology.n_atoms

                else:
                    raise Exception("ligand_input file format is not supported. supported formats: .sdf, .pdb, .cif")

            else:
                _logger.warning(f'ligand filetype not recognised. Please provide a path to a .pdb or .sdf file')
                return

            if is_solvated:
                complex_topology = ligand_topology
                complex_positions = ligand_positions
            else:
                # Now create a complex topology
                complex_md_topology = protein_md_topology.join(ligand_md_topology)
                complex_topology = complex_md_topology.to_openmm()
                complex_positions = unit.Quantity(np.zeros([protein_n_atoms + ligand_n_atoms, 3]), unit=unit.nanometers)
                complex_positions[:protein_n_atoms, :] = protein_positions
                complex_positions[protein_n_atoms:, :] = ligand_positions

                # Convert positions back to openmm vec3 objects
                complex_positions_vec3 = []
                for position in complex_positions:
                    complex_positions_vec3.append(openmm.Vec3(*position.value_in_unit_system(unit.md_unit_system)))
                complex_positions = unit.Quantity(value=complex_positions_vec3, unit=unit.nanometer)

        # Now create a system_generator
        self.system_generator = SystemGenerator(forcefields=forcefield_files,
                                                barostat=barostat,
                                                forcefield_kwargs=forcefield_kwargs,
                                                periodic_forcefield_kwargs=periodic_forcefield_kwargs,
                                                nonperiodic_forcefield_kwargs=nonperiodic_forcefield_kwargs,
                                                small_molecule_forcefield=small_molecule_forcefields,
                                                molecules=molecules,
                                                cache=None)

        # Solvate apo and complex (if necessary) and generate systems...
        inputs = []
        topology_list = [protein_topology]
        positions_list = [protein_positions]
        if ligand_input:
            topology_list.append(complex_topology)
            positions_list.append(complex_positions)

        for topology, positions in zip(topology_list, positions_list):
            if is_solvated or is_vacuum:
                solvated_topology = topology
                solvated_positions = unit.quantity.Quantity(value=np.array([list(atom_pos) for atom_pos in positions.value_in_unit_system(unit.md_unit_system)]), unit=unit.nanometers)
            else:
                solvated_topology, solvated_positions = self._solvate(topology, positions, water_model, ionic_strength, padding, box_shape)
            solvated_system = self.system_generator.create_system(solvated_topology)
            inputs.append([solvated_topology, solvated_positions, solvated_system])

        # Create a geometry engine
        geometry_engine = FFAllAngleGeometryEngine(metadata=None,
                                                use_sterics=False,
                                                n_bond_divisions=100,
                                                n_angle_divisions=180,
                                                n_torsion_divisions=360,
                                                verbose=True,
                                                storage=None,
                                                bond_softening_constant=1.0,
                                                angle_softening_constant=1.0,
                                                neglect_angles = False,
                                                use_14_nonbondeds = True)

        # Generate topology proposal, geometry proposal, and hybrid factory
        htfs = []
        for is_complex, (top, pos, sys) in enumerate(inputs):
            # Change the name of the old residue to its nonstandard name, if necessary
            # Note this needs to happen after generating the system, as the system generator requires standard residue names
            if old_residue:
                for residue in top.residues():
                    if residue.id == mutation_residue_id:
                        residue.name = old_residue
                        print(f"Changed resid {mutation_residue_id} to {residue.name}")

            # Create a topology proposal
            point_mutation_engine = PointMutationEngine(wildtype_topology=top,
                                                                 system_generator=self.system_generator,
                                                                 chain_id=mutation_chain_id, # Denote the chain id allowed to mutate (it's always a string variable)
                                                                 max_point_mutants=1,
                                                                 residues_allowed_to_mutate=[mutation_residue_id], # The residue ids allowed to mutate
                                                                 allowed_mutations=[(mutation_residue_id, proposed_residue)], # The residue ids allowed to mutate with the three-letter code allowed to change
                                                                 aggregate=True) # Always allow aggregation

            topology_proposal = point_mutation_engine.propose(sys, top, extra_sidechain_map=extra_sidechain_map, demap_CBs=demap_CBs)

            # Fix naked charges in old and new systems
            old_topology_atom_map = {atom.index: atom.residue.name for atom in topology_proposal.old_topology.atoms()}
            new_topology_atom_map = {atom.index: atom.residue.name for atom in topology_proposal.new_topology.atoms()}
            for i, system in enumerate([topology_proposal.old_system, topology_proposal.new_system]):
                force_dict = {i.__class__.__name__: i for i in system.getForces()}
                atom_map = old_topology_atom_map if i == 0 else new_topology_atom_map
                if 'NonbondedForce' in [k for k in force_dict.keys()]:
                    nb_force = force_dict['NonbondedForce']
                    for idx in range(nb_force.getNumParticles()):
                        if atom_map[idx] in ['HOH', 'WAT']: # Do not add naked charge fix to water hydrogens
                            continue
                        charge, sigma, epsilon = nb_force.getParticleParameters(idx)
                        if sigma == 0*unit.nanometer:
                            new_sigma = 0.06*unit.nanometer
                            nb_force.setParticleParameters(idx, charge, new_sigma, epsilon)
                            _logger.info(f"Changed particle {idx}'s sigma from {sigma} to {new_sigma}")
                        if epsilon == 0*unit.kilojoule_per_mole:
                            new_epsilon = 0.0001*unit.kilojoule_per_mole
                            nb_force.setParticleParameters(idx, charge, sigma, new_epsilon)
                            _logger.info(f"Changed particle {idx}'s epsilon from {epsilon} to {new_epsilon}")
                            if sigma == 1.0 * unit.nanometer: # in protein.ff14SB, hydroxyl hydrogens have sigma=1 and epsilon=0
                                new_sigma = 0.1*unit.nanometer
                                nb_force.setParticleParameters(idx, charge, new_sigma, epsilon)
                                _logger.info(f"Changed particle {idx}'s sigma from {sigma} to {new_sigma}")

            # Generate geometry proposal
            # Note: We only validate energy bookkeeping if the WT and proposed residues do not involve rings
            # We don't validate energies for geometry proposals involving ring amino acids because we insert biasing torsions
            # for ring transformations (to ensure the amino acids are somewhat in the right geometry), which will corrupt the energy addition during energy validation.
            old_res = [res for res in top.residues() if res.id == mutation_residue_id][0]
            validate_bool = False if old_res.name in ring_amino_acids or proposed_residue in ring_amino_acids else True
            new_positions, logp_proposal = geometry_engine.propose(topology_proposal, pos, beta, validate_energy_bookkeeping=validate_bool)
            logp_reverse = geometry_engine.logp_reverse(topology_proposal, new_positions, pos, beta, validate_energy_bookkeeping=validate_bool)

            # Add counterion(s) for charge changing mutations
            if not is_vacuum and transform_waters_into_ions_for_charge_changes:
                self._handle_charge_changes(topology_proposal, new_positions)
            else:
                _logger.info(f"Skipping counterion")

            # Create hybrid factories
            if generate_unmodified_hybrid_topology_factory:
                repartitioned_endstate = None
                self.generate_htf(HybridTopologyFactory, topology_proposal, pos, new_positions, flatten_exceptions, flatten_torsions, repartitioned_endstate, is_complex, rest_radius, w_lifting)
            if generate_repartitioned_hybrid_topology_factory:
                for repartitioned_endstate in [0, 1]:
                    self.generate_htf(RepartitionedHybridTopologyFactory, topology_proposal, pos, new_positions, flatten_exceptions, flatten_torsions, repartitioned_endstate, is_complex, rest_radius, w_lifting)
            if generate_rest_capable_hybrid_topology_factory:
                repartitioned_endstate = None
                if rest_radius is None:
                    _logger.info("Trying to generate a RESTCapableHybridTopologyFactory, but rest_radius was not specified. Using 0.2 nm...")
                    rest_radius = 0.2
                self.generate_htf(RESTCapableHybridTopologyFactory, topology_proposal, pos, new_positions, flatten_exceptions, flatten_torsions, repartitioned_endstate, is_complex, rest_radius, w_lifting)

            # Gather energies needed for validating endstate energies
            if not topology_proposal.unique_new_atoms:
                assert geometry_engine.forward_final_context_reduced_potential == None, f"There are no unique new atoms but the geometry_engine's final context reduced potential is not None (i.e. {self._geometry_engine.forward_final_context_reduced_potential})"
                assert geometry_engine.forward_atoms_with_positions_reduced_potential == None, f"There are no unique new atoms but the geometry_engine's forward atoms-with-positions-reduced-potential in not None (i.e. { self._geometry_engine.forward_atoms_with_positions_reduced_potential})"
            else:
                added_valence_energy = geometry_engine.forward_final_context_reduced_potential - geometry_engine.forward_atoms_with_positions_reduced_potential

            if not topology_proposal.unique_old_atoms:
                assert geometry_engine.reverse_final_context_reduced_potential == None, f"There are no unique old atoms but the geometry_engine's final context reduced potential is not None (i.e. {self._geometry_engine.reverse_final_context_reduced_potential})"
                assert geometry_engine.reverse_atoms_with_positions_reduced_potential == None, f"There are no unique old atoms but the geometry_engine's atoms-with-positions-reduced-potential in not None (i.e. { self._geometry_engine.reverse_atoms_with_positions_reduced_potential})"
                subtracted_valence_energy = 0.0
            else:
                subtracted_valence_energy = geometry_engine.reverse_final_context_reduced_potential - geometry_engine.reverse_atoms_with_positions_reduced_potential

            # Conduct endstate energy validation
            if conduct_endstate_validation:
                assert not flatten_torsions and not flatten_exceptions, "Cannot conduct endstate validation if flatten_torsions or flatten_exceptions is True"

                if generate_unmodified_hybrid_topology_factory:
                    from perses.dispersed.utils import validate_endstate_energies
                    htf = self.get_complex_htf() if is_complex else self.get_apo_htf()
                    zero_state_error, one_state_error = validate_endstate_energies(htf._topology_proposal,
                                                                                   htf,
                                                                                   added_valence_energy,
                                                                                   subtracted_valence_energy,
                                                                                   beta=beta,
                                                                                   ENERGY_THRESHOLD=ENERGY_THRESHOLD)
                if generate_repartitioned_hybrid_topology_factory:
                    from perses.dispersed.utils import validate_endstate_energies
                    htf_0 = self.get_complex_rhtf_0() if is_complex else self.get_apo_rhtf_0()
                    htf_1 = self.get_complex_rhtf_1() if is_complex else self.get_apo_rhtf_1()
                    zero_state_error, _ = validate_endstate_energies(htf_0._topology_proposal,
                                                                     htf_0,
                                                                     added_valence_energy,
                                                                     subtracted_valence_energy,
                                                                     ENERGY_THRESHOLD=ENERGY_THRESHOLD,
                                                                     beta=beta,
                                                                     repartitioned_endstate=0)
                    _, one_state_error = validate_endstate_energies(htf_1._topology_proposal,
                                                                    htf_1,
                                                                    added_valence_energy,
                                                                    subtracted_valence_energy,
                                                                    ENERGY_THRESHOLD=ENERGY_THRESHOLD,
                                                                    beta=beta,
                                                                    repartitioned_endstate=1)
                if generate_rest_capable_hybrid_topology_factory:
                    from perses.dispersed.utils import validate_endstate_energies_point
                    for endstate in [0, 1]:
                        htf = self.get_complex_rest_htf() if is_complex else self.get_apo_rest_htf()
                        validate_endstate_energies_point(htf, endstate=endstate, minimize=True)
            else:
                pass

    def generate_htf(self, factory, topology_proposal, old_positions, new_positions, flatten_exceptions, flatten_torsions, repartitioned_endstate, is_complex, rest_radius, w_lifting):
        """
        Generate hybrid factory.

        Parameters
        ----------
        factory : Callable
            name of the hybrid factory of which to generate. allowed options: 'HybridTopologyFactory',
            'RepartitionedHybridTopologyFactory', 'RESTCapableHybridTopologyFactory'
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            topology proposal to be used for generating the factory
        old_positions : np.ndarray(N, 3)
            positions (nm) of atoms corresponding to old_topology
        new_positions : np.ndarray(N, 3)
            positions (nm) of atoms corresponding to new_topology
        flatten_exceptions : bool
            whether to flatten exceptions in the HybridTopologyFactory
        flatten_torsions : bool
            whether to flatten torsions in the HybridTopologyFactory
        repartitioned_endstate : int
            endstate at which to generate the hybrid factory, only when generating a RepartitionedHybridTopologyFactory
        is_complex : bool
            if False, the factory is generated for the apo protein
            otherwise, the factory is generated for the complex
        rest_radius : unit.nanometer, default 0.3 * unit.nanometer
            radius for rest region, in nanometers
        w_lifting : unit.nanometer, default 0.3 * unit.nanometer
            maximum distance to use for the 4th dimension lifting, in nanometers

        """

        htf = factory(topology_proposal=topology_proposal,
                                      current_positions=old_positions,
                                      new_positions=new_positions,
                                      use_dispersion_correction=False,
                                      functions=None,
                                      softcore_alpha=None,
                                      bond_softening_constant=1.0,
                                      angle_softening_constant=1.0,
                                      soften_only_new=False,
                                      neglected_new_angle_terms=[],
                                      neglected_old_angle_terms=[],
                                      softcore_LJ_v2=True,
                                      softcore_electrostatics=True,
                                      softcore_LJ_v2_alpha=0.85,
                                      softcore_electrostatics_alpha=0.3,
                                      softcore_sigma_Q=1.0,
                                      interpolate_old_and_new_14s=flatten_exceptions,
                                      omitted_terms=None,
                                      endstate=repartitioned_endstate,
                                      flatten_torsions=flatten_torsions,
                                      rest_radius=rest_radius,
                                      w_lifting=w_lifting)
        if is_complex:
            if factory == HybridTopologyFactory:
                self.complex_htf = htf
            elif factory == RESTCapableHybridTopologyFactory:
                self.complex_rest_htf = htf
            elif factory == RepartitionedHybridTopologyFactory:
                if repartitioned_endstate == 0:
                    self.complex_rhtf_0 = htf
                elif repartitioned_endstate == 1:
                    self.complex_rhtf_1 = htf
        else:
            if factory == HybridTopologyFactory:
                self.apo_htf = htf
            elif factory == RESTCapableHybridTopologyFactory:
                self.apo_rest_htf = htf
            elif factory == RepartitionedHybridTopologyFactory:
                if repartitioned_endstate == 0:
                    self.apo_rhtf_0 = htf
                elif repartitioned_endstate == 1:
                    self.apo_rhtf_1 = htf

    def get_complex_htf(self):
        """
        Returns
        -------
        self.complex_htf
            complex HybridTopologyFactory
        """
        return self.complex_htf

    def get_apo_htf(self):
        """
        Returns
        -------
        self.apo_htf
            apo protein HybridTopologyFactory
        """
        return self.apo_htf

    def get_complex_rhtf_0(self):
        """
        Returns
        -------
        self.complex_rhtf_0
            complex RepartitionedHybridTopologyFactory at lambda = 0 endstate
        """
        return self.complex_rhtf_0

    def get_apo_rhtf_0(self):
        """
        Returns
        -------
        self.apo_rhtf_0
            apo protein RepartitionedHybridTopologyFactory at lambda = 0 endstate
        """
        return self.apo_rhtf_0

    def get_complex_rhtf_1(self):
        """
        Returns
        -------
        self.complex_rhtf_1
            complex RepartitionedHybridTopologyFactory at lambda = 1 endstate
        """
        return self.complex_rhtf_1

    def get_apo_rhtf_1(self):
        """
        Returns
        -------
        self.apo_rhtf_1
            apo protein RepartitionedHybridTopologyFactory at lambda = 1 endstate
        """
        return self.apo_rhtf_1

    def get_apo_rest_htf(self):
        """
        Returns
        -------
        self.apo_rest_htf
            apo protein RESTCapableHybridTopologyFactory
        """
        return self.apo_rest_htf

    def get_complex_rest_htf(self):
        """
        Returns
        -------
        self.complex_rest_htf
            complex RESTCapableHybridTopologyFactory
        """
        return self.complex_rest_htf

    def _solvate(self,
               topology,
               positions,
               water_model,
               ionic_strength,
               padding,
               box_shape):
        """
        Generate solvated topology and positions for a given input topology and positions.

        Parameters
        ----------
        topology : app.Topology
            Topology of the system to solvate
        positions : [n, 3] ndarray of Quantity nm
            the positions of the unsolvated system
        water_model : str
            solvent model to use for solvation
        ionic_strength : float * unit.molar
            the total concentration of ions (both positive and negative) to add using Modeller.
            This does not include ions that are added to neutralize the system.
            Note that only monovalent ions are currently supported.
        padding : float * unit.nanometers
            the solvent box padding
        box_shape : str
            the solvent box shape, allowed options: 'cube', 'octahedron', 'dodecahedron'

        Returns
        -------
        solvated_topology : app.Topology
            Solvated topology
        solvated_positions : [n + 3(n_waters), 3] ndarray of Quantity nm
            Solvated positions

        """
        # Create a modeller
        modeller = app.Modeller(topology, positions)

        # Add solvent
        _logger.info(f"solvating at {ionic_strength} using {water_model}")
        try: # OpenMM > 7.7
            modeller.addSolvent(self.system_generator.forcefield, model=water_model, padding=padding, boxShape=box_shape, ionicStrength=ionic_strength)
        except: # OpenMM <= 7.7
            if box_shape == 'cube':
                _logger.info("Using default box shape...")
                modeller.addSolvent(self.system_generator.forcefield, model=water_model, padding=padding, ionicStrength=ionic_strength)
            elif box_shape == 'octahedron':
                _logger.info("Attempting to manually create the truncated octahedron...")
                # Adapted from here: https://github.com/openmm/openmm/issues/3124#issuecomment-847268111
                geom_padding = padding
                max_size = max(max((pos[i] for pos in positions)) - min((pos[i] for pos in positions)) for i in range(3))
                vectors = openmm.Vec3(1, 0, 0), openmm.Vec3(1 / 3, 2 * np.sqrt(2) / 3, 0), openmm.Vec3(-1 / 3, np.sqrt(2) / 3, np.sqrt(6) / 3)
                box_vectors = [(max_size + geom_padding) * v for v in vectors]
                modeller.addSolvent(self.system_generator.forcefield, model=water_model, boxVectors=box_vectors, ionicStrength=ionic_strength)
            elif box_shape == 'dodecahedron':
                openmm_version = pkg_resources.get_distribution("openmm").version
                raise Exception(f'Dodecahedron box shape is not available in {openmm_version}')

        # Retrieve topology and positions
        solvated_topology = modeller.getTopology()
        solvated_positions = modeller.getPositions()

        # Canonicalize the solvated positions: turn tuples into np.array
        solvated_positions = unit.quantity.Quantity(value=np.array([list(atom_pos) for atom_pos in solvated_positions.value_in_unit_system(unit.md_unit_system)]), unit=unit.nanometers)

        return solvated_topology, solvated_positions

    def _handle_charge_changes(self, topology_proposal, new_positions):
        """
        Modifies the atom mapping in the topology proposal and the new system parameters to handle the transformation of
        waters into appropriate counterions for a charge-changing transformation

        Parameters
        ----------
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            topology proposal to modify
        new_positions : np.ndarray(N, 3)
            positions (nm) of atoms corresponding to new_topology
            used to determine which water(s) to turn into counterion(s)
        """
        from perses.utils.charge_changing import (get_ion_and_water_parameters,
                                                  transform_waters_into_ions,
                                                  get_water_indices,
                                                  modify_atom_classes)

        # Retrieve the charge difference between the old and new residues
        charge_diff = PointMutationEngine._get_charge_difference(topology_proposal.old_topology.residue_topology.name,
                                                                 topology_proposal.new_topology.residue_topology.name)
        if charge_diff != 0:

            # Choose water(s) to turn into ion(s)
            new_water_indices_to_ionize = get_water_indices(charge_diff=charge_diff,
                                                            new_positions=new_positions,
                                                            new_topology=topology_proposal.new_topology,
                                                            radius=0.8)
            _logger.info(f"new water indices to ionize {new_water_indices_to_ionize}")

            # Retrieve the ion and water parameters based on the ions/waters in the old system
            particle_parameters = get_ion_and_water_parameters(system=topology_proposal.old_system,
                                                               topology=topology_proposal.old_topology,
                                                               positive_ion_name="NA",
                                                               negative_ion_name="CL",
                                                               water_name="HOH")

            # Modify the nonbonded parameters of the selected water(s) in the new system
            transform_waters_into_ions(water_atoms=new_water_indices_to_ionize,
                                       system=topology_proposal._new_system,
                                       charge_diff=charge_diff,
                                       particle_parameter_dict=particle_parameters)

            # Modify the topology proposal's atom maps and and atom classes
            modify_atom_classes(new_water_indices_to_ionize, topology_proposal)
