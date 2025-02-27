import networkx as nx
import bagpype.parameters
import bagpype.molecules
import bagpype.covalent
from bagpype.errors import GraphConstructionError, MissingEnergyError, UnusualHydrogenError
from bagpype.settings import capping_decimals
import scipy
import numpy as np
import time
import itertools
import pandas as pd


#####################################
#                                   #
# The following functions deal with #
# generation of the protein graph   #
#                                   #
#####################################


class Graph_constructor(object):
    """This class is responsible for construction of the atomistic
    graph of a protein.  It identifies covalent bonds, hydrogen
    bonds, hydrophobic interactions, stacking interactions in DNA and
    various types of electrostatic interactions.
    Atom data should already have been loaded into the protein.
    """

    def __init__(self):

        ### Covalent bond related parameters:
        self.use_morse = False
        self.morse_well_width = 1.0

        ### Hydrogen bond related parameters:
        # old values commented at the very right
        self.H_acceptor_cutoff = 4  # 2.6
        self.donor_acceptor_cutoff = 5  # 3.6
        self.donor_H_acceptor_min_angle = 100  # 90
        self.donor_H_acceptor_max_angle = 180
        self.H_acceptor_neighbour_cutoff = 80

        self.H_bond_energy_cutoff = -0.01

        ### Hydrophobic interactions related parameters:
        self.hydrophobic_RMST_gamma = 0.1
        self.hydrophobic_neighbourhood = 1
        self.hydrophobic_burn_bridges = False
        self.hydrophobic_k_core = 0

        # Used to initialise all possible bonds, should always be LARGER than the longest possible bond
        self.max_cutoff = 9

        # Currently obsolete.
        # self.k_factor = 6.022 / 4.184

        # List of all bonds
        self.bonds = bagpype.molecules.BondList()

    def construct_graph(
        self,
        protein,
        atoms_file_name="atoms.csv",
        bonds_file_name="bonds.csv",
        gexf_file_name=None,
        barebones_graph=False,
    ):
        """This is the main driver function which calls
        sub-routines to generate each of the different types
        of bonds.
        The graph should already have atom data loaded.

        Options:
        atoms_file_name: specify file path for output file of atoms in csv format
        bonds_file_name: specify file path for output file of bonds in csv format
        gexf_file_name: specify file path for output file of graph in gexf format
        barebones_graph: Boolean specifying if you want a graph with no nodes/edges data
        """

        print()
        print("Graph construction started")

        self.protein = protein

        time1 = time.time()

        self._initialise_possible_bonds()
        self._initialise_covbond_dictionary()
        if self.protein.LINKs:
            self._process_LINKs()

        # Start finding covalent bonds & interactions
        self.find_covalent_bonds()
        self.find_covalent_LINK_bonds()

        # This has to be done so that the other functions can use the covalent bonds too:
        self.covalent_bonds_graph = nx.Graph()
        self.covalent_bonds_graph.add_nodes_from(
            self.protein.atoms.id()
        )  # Added later, not sure if this is a good idea...
        for bond in self.bonds:
            if bond.bond_type == ["COVALENT"]:
                self.covalent_bonds_graph.add_edge(bond.atom1.id, bond.atom2.id)

        # The following functions require knowledge of covalent bonds!
        self.find_hydrogen_bonds()
        self.find_hydrophobics()
        self.find_stacked()
        self.find_DNA_backbone()
        self.find_electrostatics()

        # Make list of bonds unique in the sense that there is only
        # one bond per pair of atoms (that can include multiple
        # contributions from hydrophobic, hydrogen, electrostatics etc.)
        self.bonds = uniquify(self.bonds)

        graph = nx.Graph()
        if barebones_graph:
            graph.add_nodes_from(self.protein.atoms.id())
        else:
            for atom in self.protein.atoms:
                graph.add_node(
                    atom.id,
                    name=atom.name,
                    chain=atom.chain,
                    res_num=atom.res_num,
                    res_name=atom.res_name,
                    element=atom.element,
                    xyz=atom.xyz,
                )

        # Construct graph from list of bonds
        id_counter = 0
        for bond in self.bonds:
            bond.id = id_counter
            if barebones_graph:
                graph.add_edge(bond.atom1.id, bond.atom2.id)
            else:
                graph.add_edge(
                    bond.atom1.id,
                    bond.atom2.id,
                    weight=bond.weight,
                    id=id_counter,
                    bond_type=bond.bond_type,
                )
            id_counter += 1

        # Check graph is connected
        number_connected_components = nx.number_connected_components(graph)
        if number_connected_components > 1:
            print(
                "WARNING: Number of connected components is greater than 1. (It's %d)"
                % (number_connected_components)
            )

        self.protein.bonds = self.bonds
        self.protein.graph = graph

        if atoms_file_name is not None:
            self._write_atoms_to_csv_file(atoms_file_name)
        if bonds_file_name is not None:
            self._write_bonds_to_csv_file(bonds_file_name)

        if gexf_file_name is not None:
            graph_modded = graph.copy()
            for _, __, d in graph_modded.edges(data=True):
                d["bond_type"] = "".join(d["bond_type"])
            nx.write_gexf(graph_modded, gexf_file_name)

        time2 = time.time()

        print(
            "Finished constructing the graph!"
            + " #residues = {}, #atoms = {}, #bonds = {}".format(
                len(self.protein.residues), len(self.protein.atoms), len(self.protein.bonds)
            )
        )
        print(
            "    Time taken = %.2fs, final filename: %s"
            % (time2 - time1, self.protein.pdb_id)
        )
        print()

    def _initialise_possible_bonds(self):
        """
        Initialises all possible bonds (ie all atoms within certain distance of atom),
        which saves a lot of computing time later.
        Relies on the fact that atom ids run from 0 to # of atoms.

        *This is the memory efficient version, which turns out to also be faster,
        and so it is to be preferred in most cases!*
        """

        print("Initialising all possible bonds, memory efficiently...")
        atom_coords = self.protein.atoms.coordinates()

        self.possible_bonds = {}

        # One single loop through all atoms
        for atom1 in range(atom_coords.shape[0]):
            atom1_coords = atom_coords[
                np.newaxis,
                atom1,
            ]
            # find all close contacts to atom1 by computing all distances of all atoms to atom1
            neighbours = list(
                np.where(
                    scipy.spatial.distance.cdist(atom1_coords, atom_coords)
                    <= self.max_cutoff
                )[1]
            )
            neighbours.remove(atom1)
            self.possible_bonds[atom1] = neighbours

    def _initialise_covbond_dictionary(self):
        """
        This is a wrapper function to run bagpype.covalent.
        It will create a dictionary of all covalent bond energies based on a list of residues
        that are present in the structure.
        """
        list_of_present_residues = sorted(
            list(set([atom.res_name for atom in self.protein.atoms]))
        )

        self.cov_bond_energies = bagpype.covalent.generate_energies_dictionary(
            list_of_present_residues
        )

        for key in bagpype.parameters.non_standard_residues:
            self.cov_bond_energies[key] = {}
        self.cov_bond_energies.update(bagpype.parameters.non_standard_residues)

    def _write_bonds_to_csv_file(self, name):
        """
        Writes bond data to a generic csv file
        """
        bond_data = []
        for bond in self.bonds:
            bond_data.append(
                [
                    bond.id,
                    ",".join(bond.bond_type),
                    bond.weight,
                    distance_between_two_atoms(bond.atom1, bond.atom2),
                    bond.atom1.id,
                    bond.atom1.name,
                    bond.atom1.res_name,
                    bond.atom1.res_num,
                    bond.atom1.chain,
                    bond.atom2.id,
                    bond.atom2.name,
                    bond.atom2.res_name,
                    bond.atom2.res_num,
                    bond.atom2.chain,
                ]
            )
        bond_df = pd.DataFrame(
            data=bond_data,
            columns=[
                "bond_id",
                "bond_type",
                "bond_weight",
                "bond_distance",
                "atom1_id",
                "atom1_name",
                "atom1_res_name",
                "atom1_res_num",
                "atom1_chain",
                "atom2_id",
                "atom2_name",
                "atom2_res_name",
                "atom2_res_num",
                "atom2_chain",
            ],
        )
        bond_df.to_csv(name, header=True, index=False)

    def _write_atoms_to_csv_file(self, name):
        atom_data = []
        for atom in self.protein.atoms:
            atom_data.append(
                [
                    atom.id,
                    atom.PDBnum,
                    atom.name,
                    atom.element,
                    atom.chain,
                    atom.res_name,
                    atom.res_num,
                    ", ".join([str(i) for i in list(atom.xyz)]),
                ]
            )
        atom_df = pd.DataFrame(
            data=atom_data,
            columns=[
                "id",
                "PDBnum",
                "name",
                "element",
                "chain",
                "res_name",
                "res_num",
                "xyz",
            ],
        )
        atom_df.to_csv(name, header=True, index=False)

    def _process_LINKs(self):
        """
        Since LINK entries can be either covalent or electrostatic, we need
        to process them beforehand. This function will judge based on the
        distance between the two atoms whether a LINK entry is describing
        a covalent bond or electrostatic interaction.
        """
        print("Processing LINK entries...")
        LINK_bonds = self.protein.LINKs
        LINK_list = []

        for LINK_bond in LINK_bonds:
            found_atoms = [None, None]
            for i in range(2):
                found = False
                LINK_atom = LINK_bond[i]
                for atom in self.protein.atoms:
                    if (
                        LINK_atom["name"] == atom.name
                        and LINK_atom["res_name"] == atom.res_name
                        and LINK_atom["res_num"] == atom.res_num
                        and LINK_atom["chain"] == atom.chain
                    ):
                        found = True
                        found_atoms[i] = atom
                        break
                if not found:
                    print(
                        (
                            "WARNING: LINK atom {0} {1} {2} {3} not found".format(
                                LINK_atom["name"],
                                LINK_atom["res_name"],
                                LINK_atom["res_num"],
                                LINK_atom["chain"],
                            )
                            + ". Ignoring this LINK entry."
                        )
                    )
                    found = False
            if found_atoms[0] and found_atoms[1]:
                is_covalent = within_cov_bonding_distance(found_atoms[0], found_atoms[1])
                new_LINK_entry = (
                    found_atoms,
                    {"is_covalent": is_covalent, "distance": LINK_bond[2]},
                )

                # Check for duplicates and don't add new_LINK_entry if already present.
                if not any(
                    [
                        found_atoms[0] == item[0][0] and found_atoms[1] == item[0][1]
                        for item in LINK_list
                    ]
                ):
                    LINK_list.append(new_LINK_entry)
        self._LINK_list = LINK_list

    ##################
    # COVALENT BONDS #
    ##################
    def find_covalent_bonds(self):
        """
        Self-explanatory function. Finds covalent bonds by looping through
        all atoms once and considering all possible neighbours in a radius
        for covalent bonds, depending on whether they are in the same or
        different residues.
        """
        print("Finding covalent bonds...")

        for atom1 in self.protein.atoms:
            for atom2 in self.protein.atoms[self.possible_bonds[atom1.id]]:
                if atom2.id > atom1.id:

                    if in_same_residue(atom1, atom2):
                        self._add_intra_residue_bond(atom1, atom2)
                    else:
                        self._add_inter_residue_bond(atom1, atom2)

    def _add_covalent_bond(self, atom1, atom2, bond_strength, bond_type="COVALENT"):
        """
        The generic function to add covalent bonds.
        """
        if bond_strength is not None:
            # bond_strength *= self.k_factor / 6.022
            bond_strength /= 4.184
            bond_strength *= self._morse_potential(atom1, atom2) if self.use_morse else 1
            self.bonds.append(
                bagpype.molecules.Bond([], atom1, atom2, bond_strength, bond_type)
            )

    def _morse_potential(self, atom1, atom2):
        """
        A short function to implement the Morse potential for covalent bonds.
        This is not usually used, but implemented as an option to explore anyways.
        """
        delta_r = abs(
            distance_between_two_atoms(atom1, atom2) - equilibrium_distance(atom1, atom2)
        )
        return 1 - (1 - np.exp(-self.morse_well_width * delta_r)) ** 2

    def _add_intra_residue_bond(self, atom1, atom2):
        """
        Sub-routine for adding intra-residue bonds. It will check the
        covalent bond dictionary for known covalent bonds, and if that
        fails, it will potentially add them based on distance constraints
        anyway.
        """
        # this is just a sanity check
        residue = atom1.res_name
        if atom2.res_name != residue:
            raise GraphConstructionError(
                "Something went wrong, trying to add intra-residue bond for two atoms of different residue names. "
            )

        try:
            # Checking only one way is enough, because covalent bond energies are generated both ways round by default.
            bond_strength = self.cov_bond_energies[residue][atom1.name][
                atom2.name
            ]  # if within_cov_bonding_distance(atom1, atom2) else None
        except KeyError:
            bond_strength = self._add_covalent_bonds_using_distance_contraints(
                atom1, atom2, print_warnings=True
            )

        self._add_covalent_bond(atom1, atom2, bond_strength)

    def _add_inter_residue_bond(self, atom1, atom2):
        """
        Sub-routine for adding INTER-residue covalent bonds.
        These are only allowed for the three types listed below.
        """
        # peptide bond, disulfide bridge and nucleic bond
        conditions = (
            (atom1.element == "C" and atom2.element == "N")
            or (atom1.element == "N" and atom2.element == "C")
            or (atom1.element == "O" and atom2.element == "P")
            or (atom1.element == "P" and atom2.element == "O")
        )

        # Distinguish between covalent bonds and DISULFIDE bridges! O.o
        if conditions:
            bond_type = "COVALENT"
        elif atom1.element == "S" and atom2.element == "S":
            bond_type = "DISULFIDE"
        else:
            bond_type = None

        if bond_type is not None:
            bond_strength = self._add_covalent_bonds_using_distance_contraints(atom1, atom2)
            self._add_covalent_bond(atom1, atom2, bond_strength, bond_type=bond_type)

    def _add_covalent_bonds_using_distance_contraints(
        self, atom1, atom2, print_warnings=False
    ):
        """
        This function will check whether two atoms are within covalent
        bonding distance, and then attempt to lookup the corresponding
        bond energy from the parameters file.
        """
        if within_cov_bonding_distance(atom1, atom2):
            lookup = tuple([atom1.element, atom2.element])
            lookup_reversed = tuple([atom2.element, atom1.element])
            if print_warnings:
                print(
                    "WARNING: Adding bond between {} and {} based on distance constraints using single bond energies!".format(
                        atom1, atom2
                    )
                )

            # Look up the single bond energy and if not found, raise an error
            try:
                return bagpype.parameters.single_bond_energies[lookup]
            except KeyError:
                try:
                    return bagpype.parameters.single_bond_energies[lookup_reversed]
                except KeyError:
                    raise MissingEnergyError(
                        atom1.element,
                        atom2.element,
                        (atom1.res_name + atom1.res_num, atom2.res_name + atom2.res_num),
                    )
        else:
            return None

    def find_covalent_LINK_bonds(self):
        """
        This auxiliary sub_routine will process covalent LINK entries.
        """
        # Covalent LINK entries
        if self.protein.LINKs:
            are_there_LINK_covalent_bonds = bool(
                sum([x[1]["is_covalent"] for x in self._LINK_list])
            )
            if are_there_LINK_covalent_bonds:
                print("    Considering covalent LINK entries...")

                # To avoid doubly adding covalent LINK entries where they have already been detected, we need the following list of covalent bonds
                cov_bonds = [
                    (bond.atom1.id, bond.atom2.id)
                    for bond in self.bonds
                    if "COVALENT" in bond.bond_type
                ]
                cov_bonds.extend([bond[::-1] for bond in cov_bonds])

                for item in self._LINK_list:
                    atom1, atom2 = item[0]

                    # Is this LINK entry covalent and is this bond not already included?
                    if item[1]["is_covalent"] and (atom1.id, atom2.id) not in cov_bonds:

                        bond_strength = self._add_covalent_bonds_using_distance_contraints(
                            atom1, atom2, print_warnings=True
                        )
                        self._add_covalent_bond(atom1, atom2, bond_strength)

    ##################
    # HYDROGEN BONDS #
    ##################

    def find_hydrogen_bonds(self):
        """
        Top-level wrapper funciton to determine hydrogen bonds
        """
        print(
            "Finding hydrogen bonds at cutoff = "
            + str(self.H_bond_energy_cutoff)
            + "kcal/mol..."
        )
        total_hydrogen_energy = 0

        print("    Assigning H-bond status...")
        # Assign H-bond status to all N, O, S atoms in the structure
        # Very efficient, so ok if some might not get used
        self.Hbond_status = {}
        for atom in self.protein.atoms:
            if atom.element in ["N", "O", "S"]:
                self.Hbond_status[atom.id] = self._assign_Hbond_status(atom)

        print("    Applying constraints and computing bond strengths...")
        # This is where hydrogen bonds are identified
        for hydrogen in [atom for atom in self.protein.atoms if atom.element == "H"]:
            donor = self._find_donor(hydrogen)
            # some preliminary conditions for the donor to not be eligible
            if (
                donor.element not in ["N", "O", "S"]
                or self.Hbond_status[donor.id] == None
                or donor.name in ["OXT"]
            ):
                continue

            # Now loop through all possible acceptors
            for acceptor in [
                atom
                for atom in self.protein.atoms[self.possible_bonds[hydrogen.id]]
                if atom.element in ["N", "O", "S"]
            ]:

                if self.Hbond_status[acceptor.id] == None or acceptor.name in ["OXT"]:
                    continue

                # Some more conditions for the triple to eligible
                if (
                    self._is_donor(donor)
                    and self._is_acceptor(acceptor)
                    and not in_third_neighbourhood(
                        self.covalent_bonds_graph, hydrogen, acceptor
                    )
                ):
                    # Check criteria:
                    r = distance_between_two_atoms(acceptor, hydrogen)
                    d = distance_between_two_atoms(acceptor, donor)

                    if d < self.donor_acceptor_cutoff and r < self.H_acceptor_cutoff:
                        theta = self._get_angle(donor, hydrogen, acceptor)

                        if (
                            self.donor_H_acceptor_min_angle
                            <= np.rad2deg(theta)
                            <= self.donor_H_acceptor_max_angle
                        ):
                            # Is this a salt bridge?
                            salt_bridge_indicator = self._is_salt_bridge(
                                hydrogen, donor, acceptor
                            )

                            if salt_bridge_indicator:
                                bond_strength = self._compute_salt_bridge_energy(d)
                            else:
                                bond_strength = self._compute_hydrogen_bond_energy(
                                    hydrogen, donor, acceptor, d, theta
                                )

                            if bond_strength is not None and (
                                bond_strength < self.H_bond_energy_cutoff
                                or (salt_bridge_indicator and bond_strength < 0)
                            ):
                                atom1, atom2 = hydrogen, acceptor

                                # bond_strength *= -self.k_factor * 4.184 / 6.022
                                bond_strength *= -1
                                total_hydrogen_energy -= bond_strength

                                self.bonds.append(
                                    bagpype.molecules.Bond(
                                        [],
                                        atom1,
                                        atom2,
                                        bond_strength,
                                        {True: "SALTBRIDGE", False: "HYDROGEN"}[
                                            salt_bridge_indicator
                                        ],
                                    )
                                )

        print(
            "    Total energy of hydrogen bonds and salt bridges: "
            + str(round(total_hydrogen_energy, 2))
            + " kcal/mol"
        )

    def _get_angle(self, atom1, atom2, atom3):
        """
        Finds the angle formed by the three input atoms, where atom2 is the vertex of the angle.
        input: three atom objects
        output: angle IN RAD!!
        """
        v21 = atom2.xyz - atom1.xyz
        v23 = atom2.xyz - atom3.xyz

        cosine_angle = np.dot(v21, v23) / (np.linalg.norm(v21) * np.linalg.norm(v23))

        # Note: as the arccos is only defined on [0, pi], we do not have to worry about this.
        return np.arccos(cosine_angle)

    def _get_out_of_plane_angle(self, donor_list, acceptor_list):
        """
        Find the angle between the two planes formed by donor_list and acceptor_list
        input: two lists of three atom ids
        output: angle IN RAD!!
        """

        def normal_unit_vector(point1, point2, point3):
            """Function to find normal unit vector for 3 points in 3D space"""
            v12 = point1 - point2
            v13 = point1 - point3
            normal_vector = np.cross(v12, v13)
            return normal_vector / np.linalg.norm(normal_vector)

        # Find normal unit vector for both planes
        donor_plane_normal = normal_unit_vector(
            self.protein.atoms[donor_list[0]].xyz,
            self.protein.atoms[donor_list[1]].xyz,
            self.protein.atoms[donor_list[2]].xyz,
        )
        acceptor_plane_normal = normal_unit_vector(
            self.protein.atoms[acceptor_list[0]].xyz,
            self.protein.atoms[acceptor_list[1]].xyz,
            self.protein.atoms[acceptor_list[2]].xyz,
        )

        # The angle is now arccos(normal1 *dot* normal2)
        gamma = np.arccos(np.dot(donor_plane_normal, acceptor_plane_normal))

        if gamma < np.pi / 2.0:
            gamma = np.pi - gamma
        return gamma

    def _find_donor(self, atom):
        """
        This identifies the covalent bonding partner of a hydrogen
        atom. There should be exactly one. This will raise an error if
        the atom is not a hydrogen or the atom has more than 1 covalent bonding
        partner.
        """
        if atom.element != "H":
            raise UnusualHydrogenError(
                "You have tried to find the donor of atom {0}, "
                "but atom {0} is not a hydrogen. ".format(atom.id)
            )

        cov_bonding_partners_ids = list(self.covalent_bonds_graph.neighbors(atom.id))
        # cov_bonding_partners_ids = [(bond.atom1.id, bond.atom2.id) for bond in covalent_bonds if bond.atom1.id == atom.id or bond.atom2.id == atom.id]
        if len(cov_bonding_partners_ids) > 1:
            raise UnusualHydrogenError(
                "Atom {0} is a hydrogen, but has more than one covalent "
                "bonding partner: {1}. ".format(
                    atom, [self.protein.atoms[x] for x in cov_bonding_partners_ids]
                )
            )
        elif len(cov_bonding_partners_ids) == 0:
            raise UnusualHydrogenError(
                "Atom {0} is a hydrogen, but has no covalent "
                "bonding partners. ".format(atom)
            )
        else:
            return self.protein.atoms[cov_bonding_partners_ids[0]]

    def _is_donor(self, atom):
        return (
            True
            if self.Hbond_status[atom.id]["DonorAcceptor"] in ("donor", "both")
            else False
        )

    def _is_acceptor(self, atom):
        return (
            True
            if self.Hbond_status[atom.id]["DonorAcceptor"] in ("acceptor", "both")
            else False
        )

    def _is_salt_bridge(self, hydrogen, donor, acceptor):
        """Check if triplet is a salt bridge"""

        # Donor and acceptor need to be charged
        if (
            self.Hbond_status[donor.id]["Charged"]
            and self.Hbond_status[acceptor.id]["Charged"]
        ):

            for n in self.covalent_bonds_graph.neighbors(acceptor.id):
                neighbor_atom = self.protein.atoms[n]
                phi = self._get_angle(hydrogen, acceptor, neighbor_atom)
                if np.rad2deg(phi) < self.H_acceptor_neighbour_cutoff:
                    return False
            return True
        else:
            return False

    def _compute_salt_bridge_energy(self, donor_acceptor_distance):
        R_s = 3.2
        x = 0.375
        V_0 = 10

        Rnorm = R_s / (donor_acceptor_distance + x)
        E = V_0 * (5 * (Rnorm ** 12) - 6 * (Rnorm ** 10))
        return E

    def _compute_hydrogen_bond_energy(
        self, hydrogen, donor, acceptor, donor_acceptor_distance, theta
    ):
        """
        Computes hydrogen bond energy according to formula which is printed in FIRST manual,
        derived from Mayo potential
        """

        D_SP2 = False
        D_SP3 = False
        A_SP2 = False
        A_SP3 = False

        donor_triplet_list = self._find_all_atom_triples(hydrogen)
        acceptor_triplet_list = self._find_all_atom_triples(acceptor)

        R_s = 2.8
        V_0 = 8.0
        Rnorm = R_s / donor_acceptor_distance

        E_distance = V_0 * (5 * (Rnorm ** 12) - 6 * (Rnorm ** 10))

        prefactor = (np.cos(theta) ** 2) * np.exp(-((np.pi - theta) ** 6))

        # Set a few booleans denoting Hybridisation, in particular D_SP2, D_SP3, A_SP2, A_SP3
        if self.Hbond_status[donor.id]["Hybridisation"] == "sp2":
            D_SP2 = True
        elif self.Hbond_status[donor.id]["Hybridisation"] == "sp3":
            D_SP3 = True
        else:
            raise GraphConstructionError(
                "Hybridisation state for atom "
                + str(donor.id)
                + " is invalid: "
                + str(self.Hbond_status[donor.id]["Hybridisation"])
            )

        if self.Hbond_status[acceptor.id]["Hybridisation"] == "sp2":
            A_SP2 = True
        elif self.Hbond_status[acceptor.id]["Hybridisation"] == "sp3":
            A_SP3 = True
        else:
            raise GraphConstructionError(
                "Hybridisation state for atom "
                + str(acceptor.id)
                + " is invalid: "
                + str(self.Hbond_status[acceptor.id]["Hybridisation"])
            )

        if D_SP2 and A_SP2:

            angle = 0.0
            best_angle = 0.0

            for donor_triplet in donor_triplet_list:
                for acceptor_triplet in acceptor_triplet_list:

                    phi = self._get_angle(
                        hydrogen, acceptor, self.protein.atoms[acceptor_triplet[1]]
                    )
                    gamma = self._get_out_of_plane_angle(donor_triplet, acceptor_triplet)

                    if np.rad2deg(phi) <= 90:
                        return None

                    if phi > gamma:
                        angle = phi
                    else:
                        angle = gamma

                    if angle > best_angle:
                        best_angle = angle

            E_angular = np.cos(best_angle) ** 2 * prefactor

        elif D_SP3 and A_SP2:

            best_phi = 0.0
            for acceptor_triplet in acceptor_triplet_list:
                phi = self._get_angle(
                    hydrogen, acceptor, self.protein.atoms[acceptor_triplet[1]]
                )

                if np.rad2deg(phi) <= 90:
                    return None

                if phi > best_phi:
                    best_phi = phi
            E_angular = np.cos(best_phi) ** 2 * prefactor

        elif D_SP2 and A_SP3:

            E_angular = prefactor ** 2

        elif D_SP3 and A_SP3:

            diff_angle = np.deg2rad(109.5)
            best_phi = 100.0

            for acceptor_triplet in acceptor_triplet_list:

                phi = self._get_angle(
                    hydrogen, acceptor, self.protein.atoms[acceptor_triplet[1]]
                )

                if phi - diff_angle > np.deg2rad(90.0):
                    return None

                if phi < best_phi:
                    best_phi = phi

            E_angular = np.cos(best_phi - diff_angle) ** 2 * prefactor

        E = E_distance * E_angular
        return E

    def _find_all_atom_triples(self, atom):
        """
        Finds all possible triplets surrounding atom.
        """
        triple_list = []

        site1 = atom.id
        degree = nx.degree(self.covalent_bonds_graph, site1)

        if degree < 1:
            raise GraphConstructionError(
                "Couldn't find triplet for single atom {} with no bonds! ".format(atom)
            )

        elif degree == 1:
            site2 = list(self.covalent_bonds_graph.neighbors(atom.id))[0]
            for site3 in self.covalent_bonds_graph.neighbors(site2):
                if site3 != site1 and (
                    self.protein.atoms[site1].res_name == "HOH"
                    or self.protein.atoms[site2].element != "H"
                ):
                    triple_list.append([site1, site2, site3])
        else:
            for site2 in self.covalent_bonds_graph.neighbors(site1):
                for site3 in self.covalent_bonds_graph.neighbors(site1):
                    if site2 != site3 and (
                        self.protein.atoms[site1].res_name == "HOH"
                        or self.protein.atoms[site2].element != "H"
                    ):
                        triple_list.append([site1, site2, site3])

        return triple_list

    def _assign_Hbond_status(self, atom):
        """Determine and assign H-bonding status to atoms
        Input: one atom object
        Writes status into a dictionary with the following possible values:
        Hybridisation: "sp2", "sp3"
        Charged: True, False
        DonorAcceptor: "donor", "acceptor", "both"
        """

        status_out = {}

        if atom.element == "N":
            if self._is_mainchain(atom):
                # tests for terminal Nitrogens
                if nx.degree(self.covalent_bonds_graph, atom.id) == 4:
                    status_out.update(
                        DonorAcceptor="donor", Charged=True, Hybridisation="sp3"
                    )
                else:
                    status_out.update(
                        DonorAcceptor="donor", Charged=False, Hybridisation="sp2"
                    )
            elif self._get_charged_residue(atom) is not None:
                status_out = self._get_charged_residue(atom)
            else:
                status_out = self._determine_status_N(atom)

        elif atom.element == "O":
            if self._is_mainchain(atom):
                status_out.update(
                    DonorAcceptor="acceptor", Charged=False, Hybridisation="sp2"
                )
            elif self._get_charged_residue(atom) is not None:
                status_out = self._get_charged_residue(atom)
            else:
                status_out = self._determine_status_O(atom)

        elif atom.element == "S":
            status_out = self._determine_status_S(atom)

        else:
            raise GraphConstructionError(
                "Function for Hbond status assignment did not receive an atom of element N, O or S. "
            )

        # perform a check:
        if status_out is not None and (
            status_out["DonorAcceptor"] not in ("donor", "acceptor", "both")
            or status_out["Charged"] not in (True, False)
            or status_out["Hybridisation"] not in ("sp2", "sp3")
        ):
            raise GraphConstructionError(
                "Atom "
                + str(atom.id)
                + " was not correctly given a Hydrogen status. Something must have gone wrong in the code. "
            )

        return status_out

    def _is_mainchain(self, atom):
        """Check if atom is on peptide mainchain"""

        neighbour_atom_names = [
            self.protein.atoms[a].name for a in self.covalent_bonds_graph.neighbors(atom.id)
        ]

        # First 3 criteria (ie CA, N and C) refer to the backbone.

        if atom.name == "CA":
            # Check for N and C as direct neighbours
            if "N" in neighbour_atom_names and "C" in neighbour_atom_names:
                return True

        elif atom.name == "N":
            # Check for CA as direct and C neighbour with dist 2
            if "CA" in neighbour_atom_names:
                sec_neighbour_ids = sec_neighborhood(self.covalent_bonds_graph, atom.id)
                sec_neighbour_names = [self.protein.atoms[a].name for a in sec_neighbour_ids]
                if "C" in sec_neighbour_names:
                    return True

        elif atom.name == "C":
            # check for CA and O as direct neighbours
            if "CA" in neighbour_atom_names and "O" in neighbour_atom_names:
                return True

        elif atom.name == "O":
            # if C is direct neighbour and CA is second nearest neighbour
            if "C" in neighbour_atom_names:
                sec_neighbour_ids = sec_neighborhood(self.covalent_bonds_graph, atom.id)
                sec_neighbour_names = [self.protein.atoms[a].name for a in sec_neighbour_ids]
                if "CA" in sec_neighbour_names:
                    return True

        elif atom.name == "H":
            # if N direct and CA second nearest neighbour
            if "N" in neighbour_atom_names:
                sec_neighbour_ids = sec_neighborhood(self.covalent_bonds_graph, atom.id)
                sec_neighbour_names = [self.protein.atoms[a].name for a in sec_neighbour_ids]
                if "CA" in sec_neighbour_names:
                    return True

        # For DNA, RNA...
        # but this case will never actually come up...
        elif atom.name == "P":
            # if O5 direct neighbour
            if "O5" in neighbour_atom_names:
                return True

        # Default
        return False

    def _get_charged_residue(self, atom):
        """Check if atom is in a charged residue and assign orbid and charge status"""

        if atom.element == "N":
            if atom.res_name == "ARG":
                return dict(DonorAcceptor="donor", Charged=True, Hybridisation="sp2")

            elif atom.res_name == "LYS":
                return dict(DonorAcceptor="donor", Charged=True, Hybridisation="sp3")

            else:
                neighbour_atom_names = [
                    self.protein.atoms[a].name
                    for a in self.covalent_bonds_graph.neighbors(atom.id)
                ]
                if "CA" in neighbour_atom_names and "C" not in neighbour_atom_names:
                    return dict(DonorAcceptor="donor", Charged=True, Hybridisation="sp3")

        elif atom.element == "O":
            if atom.res_name == "ASP" or atom.res_name == "GLU":
                return dict(DonorAcceptor="acceptor", Charged=True, Hybridisation="sp2")

            else:
                neighbour_atom_names = [
                    self.protein.atoms[a].name
                    for a in self.covalent_bonds_graph.neighbors(atom.id)
                ]
                if "CB" not in neighbour_atom_names:
                    sec_neighbour_ids = sec_neighborhood(self.covalent_bonds_graph, atom.id)
                    sec_neighbour_names = [
                        self.protein.atoms[a].name for a in sec_neighbour_ids
                    ]
                    if "CA" in sec_neighbour_names and "N" not in sec_neighbour_names:
                        return dict(
                            DonorAcceptor="acceptor", Charged=True, Hybridisation="sp2"
                        )
        return None

    def _determine_status_N(self, atom):
        """Determines and assigns hydrogen-bonding status to Nitrogen atoms"""

        if atom.res_name == "ASN" or atom.res_name == "GLN":
            return dict(DonorAcceptor="donor", Charged=False, Hybridisation="sp2")

        elif atom.res_name == "HIS":

            hist_state = self._get_histidine_protonation_state(atom)

            if hist_state == 1 or hist_state == 3:
                return dict(DonorAcceptor="donor", Charged=True, Hybridisation="sp2")

            else:
                return dict(DonorAcceptor="acceptor", Charged=True, Hybridisation="sp2")

        elif atom.res_name == "TRP":
            return dict(DonorAcceptor="donor", Charged=False, Hybridisation="sp2")

        else:
            degree = nx.degree(self.covalent_bonds_graph, atom.id)
            if degree == 0:
                raise GraphConstructionError(
                    "The Nitrogen {} has no covalent bonding partners. ".format(atom)
                )

            elif degree == 2:
                for n in self.covalent_bonds_graph.neighbors(atom.id):

                    if self._is_double_bond(atom, self.protein.atoms[n]):
                        return dict(
                            DonorAcceptor="acceptor", Charged=False, Hybridisation="sp2"
                        )

                return dict(DonorAcceptor="acceptor", Charged=True, Hybridisation="sp2")

            elif degree == 3:
                return dict(DonorAcceptor="donor", Charged=False, Hybridisation="sp2")

            elif degree == 4:
                return dict(DonorAcceptor="donor", Charged=True, Hybridisation="sp3")

            elif degree > 4:
                print(
                    "Something went wrong, atom {0} is a Nitrogen".format(atom)
                    + "but has more than 4 covalent bonds"
                )
                return None

        # This is a relict in FIRST, this will never happen as the above covers all possible cases. Included for completeness's sake
        return dict(DonorAcceptor="donor", Charged=False, Hybridisation="sp2")

    def _get_histidine_protonation_state(self, atom):
        """Determines Histidine protonation state"""

        state = 0
        for neighbour_atom in [
            self.protein.atoms[j] for j in self.covalent_bonds_graph.neighbors(atom.id)
        ]:
            if neighbour_atom.element == "H":
                state += 1
            if neighbour_atom.name == "CE1":
                CE_atom = neighbour_atom

                for ce_neighbour_atom in [
                    self.protein.atoms[j]
                    for j in self.covalent_bonds_graph.neighbors(CE_atom.id)
                ]:
                    if ce_neighbour_atom.element == "N" and ce_neighbour_atom.id != atom.id:
                        other_nitrogen = ce_neighbour_atom
                        for other_N_neighbour_atom in [
                            self.protein.atoms[k]
                            for k in self.covalent_bonds_graph.neighbors(other_nitrogen.id)
                        ]:
                            if other_N_neighbour_atom.element == "H":
                                state += 2
        return state

    def _is_double_bond(self, atom1, atom2):
        """Check if bond is actually a double bond -- it has to be within 1.4 A"""

        return (
            True
            if (
                (atom1.element == "N" and atom2.element == "C")
                or (atom2.element == "C" and atom1.element == "N")
            )
            and distance_between_two_atoms(atom1, atom2) <= 1.4
            else False
        )

    def _determine_status_O(self, atom):
        """Determines and assigns hydrogen-bonding status to Oxygen atoms"""

        if atom.res_name == "SER" or atom.res_name == "THR":
            return dict(DonorAcceptor="both", Charged=False, Hybridisation="sp3")

        elif atom.res_name == "TYR":
            return dict(DonorAcceptor="both", Charged=False, Hybridisation="sp2")

        elif atom.res_name == "ASN" or atom.res_name == "GLN":
            # kept this case here separately in case we go for donor/acceptor assignment later..
            return dict(DonorAcceptor="acceptor", Charged=False, Hybridisation="sp2")

        else:
            degree = nx.degree(self.covalent_bonds_graph, atom.id)

            if degree == 0:
                if atom.res_name == "HOH":
                    raise GraphConstructionError(
                        "The Oxygen {} has no covalent bonding partners. "
                        "Since this O belongs to a water molecule, this error was very likely to be caused by Reduce, "
                        "a third-party software that BaGPyPe uses to add hydrogens to PDB files. Unfortunately, "
                        "Reduce is not able to add hydrogens to single Oxygen atoms (belonging to waters). "
                        "If you would like to include water molecules "
                        "in the atomistic graph, please consider using a different software to add hydrogen atoms. ".format(
                            atom.id
                        )
                    )
                else:
                    raise GraphConstructionError(
                        "The Oxygen {} has no covalent bonding partners. ".format(atom)
                    )

            if degree == 1:
                sec_neighbors_id = sec_neighborhood(self.covalent_bonds_graph, atom.id)
                other_O = None

                # check if there is a second O in neighborhood
                for i in sec_neighbors_id:
                    if self.protein.atoms[i].element == "O":
                        other_O = i
                # if there is than check if it is also single bonded
                if other_O is not None:

                    if nx.degree(self.covalent_bonds_graph, other_O) == 1:
                        return dict(
                            DonorAcceptor="acceptor", Charged=True, Hybridisation="sp2"
                        )

                    else:
                        return dict(
                            DonorAcceptor="acceptor", Charged=False, Hybridisation="sp2"
                        )
                # no other O in surroundings..
                else:
                    return dict(DonorAcceptor="acceptor", Charged=False, Hybridisation="sp2")

            elif degree == 2:
                any_neighbour_H = False
                for neighbour_atom in [
                    self.protein.atoms[j]
                    for j in self.covalent_bonds_graph.neighbors(atom.id)
                ]:
                    if neighbour_atom.element == "H":
                        any_neighbour_H = True

                if any_neighbour_H:
                    return dict(DonorAcceptor="both", Charged=False, Hybridisation="sp3")

                else:
                    return dict(DonorAcceptor="acceptor", Charged=False, Hybridisation="sp3")

            elif degree > 2:
                print(
                    "Something went wrong. Atom {0} is an Oxygen ".format(atom)
                    + "but has more than 2 covalent bonds"
                )
                return None

    def _determine_status_S(self, atom):
        """Determines and assigns hydrogen-bonding status to Sulfur atoms"""

        if atom.res_name == "MET":
            return dict(DonorAcceptor="acceptor", Charged=False, Hybridisation="sp3")

        elif atom.res_name == "CYS" or atom.res_name == "CYX":
            neighbours = self.covalent_bonds_graph.neighbors(atom.id)

            if "H" in [self.protein.atoms[n].element for n in neighbours]:
                return dict(DonorAcceptor="both", Charged=False, Hybridisation="sp3")

            else:
                return dict(DonorAcceptor="acceptor", Charged=False, Hybridisation="sp3")

        else:
            degree = nx.degree(self.covalent_bonds_graph, atom.id)

            if degree == 0:
                raise GraphConstructionError(
                    "The Sulfur {} has no covalent bonding partners. ".format(atom)
                )

            elif degree == 1:
                return dict(DonorAcceptor="acceptor", Charged=True, Hybridisation="sp2")

            elif degree == 2:
                neighbours = self.covalent_bonds_graph.neighbors(atom.id)

                if "H" in [self.protein.atoms[n].element for n in neighbours]:
                    return dict(DonorAcceptor="both", Charged=False, Hybridisation="sp3")

                else:
                    return dict(DonorAcceptor="acceptor", Charged=False, Hybridisation="sp3")

            elif degree == 3:
                return dict(DonorAcceptor="donor", Charged=True, Hybridisation="sp3")

            else:
                print(
                    "Something went wrong. Atom {0} is a Sulfur ".format(atom)
                    + "but has more than 3 covalent bonds. Couldn't determine H-bonding status."
                )
                return None

    ############################
    # HYDROPHOBIC INTERACTIONS #
    ############################

    def find_hydrophobics(self):
        """Function that implements hydrophobic interaction as a three step process: 1. Pre-selection, 2. Weighting, 3. Sparsification
        Parameters are passed as values belonging to self:
        self.hydrophobic_RMST_gamma, self.hydrophobic_neighbourhood, self.hydrophobic_burn_bridges

        Florian Song, September 2018 - April 2019
        """
        print("Finding hydrophobics...")

        # Initiate list of hydrophobic interactions

        self._all_possible_hphobic_graph = nx.Graph()

        # Loop through all atoms
        for atom1 in self.protein.atoms:

            # Requirements for atom1 to be eligible: only Carbon or Sulfur and only bonded to Carbon, Sulfur or Hydrogen
            if atom1.element in ("C", "S") and self.only_bonded_to_CSH(atom1):

                for atom2 in self.protein.atoms[self.possible_bonds[atom1.id]]:

                    # Remaining requirements for hydrophobic interactions to be feasible
                    # Python's logical and/or are short circuit evaluated,
                    # so putting all conditions in one is fine,
                    # if the most basic condition is in first place
                    conditions = (
                        atom1.id < atom2.id
                        and atom2.element in ("C", "S")
                        and self.only_bonded_to_CSH(atom2)
                        and not in_same_residue(atom1, atom2)
                        and not in_third_neighbourhood(
                            self.covalent_bonds_graph, atom1, atom2
                        )
                    )
                    if conditions:
                        distance = distance_between_two_atoms(atom1, atom2)
                        energy = self.hydrophobic_potential(distance)
                    else:
                        energy = None

                    if energy is not None:
                        self._all_possible_hphobic_graph.add_edge(
                            atom1.id,
                            atom2.id,
                            # weight=-1 * energy,
                            distance=distance,
                            energy=energy,
                        )

        # The code further down won't work if the graph is empty.
        if nx.is_empty(self._all_possible_hphobic_graph):
            return

        # Compute the RMST of all eligible hydrophobic interactions
        matches = self._hydrophobic_selection2(self._all_possible_hphobic_graph)

        if self.hydrophobic_burn_bridges:
            # Additional step to RMST sparsification: removal of graph bridges
            # This step is not recommended by default, but presents an interesting additional sparsification
            # A bridge is an edge of a graph whose deletion increases its number of connected components.
            # Note that here we do not have to worry about weighting
            # In order to use networkx, need to initialise new Graph
            rmst_graph = nx.Graph(matches)

            # Use networkx to find all bridges in the graph and remove them
            bridges = list(nx.bridges(rmst_graph))
            rmst_graph.remove_edges_from(bridges)
            matches = rmst_graph.edges

            print(
                "    Removed bridges: "
                + str(len(bridges))
                + "; Final # hydrophobic interactions: "
                + str(len(matches))
                + ", components: "
                + str(nx.number_connected_components(nx.Graph(matches)))
            )

        if self.hydrophobic_k_core:
            # Additional step to RMST sparsification: computing the k-core
            # This step is not recommended by default, but presents an interesting additional sparsification
            # A k-core is a maximal subgraph s.t. no node has degree less than k.
            # Note that here we do not have to worry about weighting
            # In order to use networkx, need to initialise new Graph
            rmst_graph = nx.Graph(matches)

            # Use networkx to prune first degree nodes off
            k = int(self.hydrophobic_k_core)
            matches = nx.k_core(rmst_graph, k).edges

            print(
                "    Removed {} k-core shell!".format(
                    str(k) + {1: "st", 2: "nd", 3: "rd"}.get(k % 10, "th")
                )
                + " Final # hydrophobic interactions: "
                + str(len(matches))
                + ", components: "
                + str(nx.number_connected_components(nx.Graph(matches)))
            )

        # Sort the final hydrophobic interactions for consistency
        matches = sorted([sorted(x) for x in matches])

        # Finally, write all bonds to self.bonds
        for bond in matches:
            atom1, atom2 = self.protein.atoms[bond[0]], self.protein.atoms[bond[1]]
            bond_strength = self._all_possible_hphobic_graph[bond[0]][bond[1]]["energy"]
            # bond_strength *= -self.k_factor * 4.184 / 6.022
            bond_strength *= -1
            self.bonds.append(
                bagpype.molecules.Bond([], atom1, atom2, bond_strength, "HYDROPHOBIC")
            )

        # Calculate total hydrophobic energy for command line output
        total_hydrophobic_energy = sum(
            [self._all_possible_hphobic_graph[i][j]["energy"] for i, j in matches]
        )
        print(
            "    Total energy of hydrophobic effect: "
            + str(round(total_hydrophobic_energy, 2))
            + " kcal/mol"
        )

    def _hydrophobic_selection_legacyNetworkx(self, graph):
        """
        This is an older, but more readable implementation of RMST using Networkx.
        Please see the next function for a much faster and more memory-efficient version.
        """
        # First step is to calculate a minimum spanning tree, note that the weight is "energy", ie negative values, units kcal/mol
        mst = nx.minimum_spanning_tree(graph, weight="energy")

        # notmst contains all edges that are in graph but not in mst
        notmst = nx.difference(graph, mst)

        # Initialise the final output list of accepted edges
        matches = sorted(mst.edges)

        # Find d_i (as defined by RMST). This is the strongest interaction originating at i.
        D = nx.adjacency_matrix(graph, weight="energy")
        d = D.min(axis=0).toarray().flatten().tolist()
        node_list = list(graph.nodes)

        for edge_not_in_mst in notmst.edges:
            i, j = edge_not_in_mst[0], edge_not_in_mst[1]

            # Find the path between i and j along the MST and the corresponding weights
            path = nx.shortest_path(mst, source=i, target=j)
            weights_along_path = [graph[u][v]["energy"] for u, v in zip(path[:-1], path[1:])]

            # mlink is the maximum of the weights along the MST path
            mlink = max(weights_along_path)

            # RMST criterion for adding edge back into the tree
            # hydrophobic_RMST_gamma is the gamma parameter as described in RMST
            left_hand_side = (
                mlink
                + self.hydrophobic_RMST_gamma
                * abs(d[node_list.index(i)] + d[node_list.index(j)])
                / 2
            )
            right_hand_side = graph[i][j]["energy"]

            if left_hand_side > right_hand_side:
                matches += [(i, j)]

        print(
            "    RMST sparsification used. Accepted: "
            + str(len(matches) - len(mst.edges))
            + ", rejected: "
            + str(len(graph.edges) - len(matches))
            + "; MST size: "
            + str(len(mst.edges))
        )

        return sorted(matches)

    def _hydrophobic_selection2(self, graph):
        """
        This function represents the sparsification step.
        Sparsification is done via a modified version of RMST (relaxed minimum spanning tree).
        Note that the computations below may seem unnecessarily complicated, but
        are by a large margin faster than the more readable Networkx implementation above.
        Reference:
        Beguerisse-Diaz, M., Vangelov, B. & Barahona, M.
        Finding role communities in directed networks using Role-Based Similarity, Markov Stability and the Relaxed Minimum Spanning Tree.
        2013 IEEE Global Conference on Signal and Information Processing 937–940 (IEEE, 2013). doi:10.1109/GlobalSIP.2013.6737046
        """

        node_list = list(graph.nodes)
        D = nx.adjacency_matrix(graph, weight="energy", nodelist=node_list).todense()
        N = np.shape(D)[0]
        node_dictionary = dict(zip(range(N), node_list))

        # Use a modified version of Prim's algorithm
        Emst, LLink = self._RMST_prim_algorithm(D)

        Dtemp = D + np.eye(N) * np.amax(D)

        mD = np.amin(Dtemp, 0)
        mD = mD.reshape(1,N)
        mD = np.abs(np.tile(mD, (N, 1)) + np.tile(np.transpose(mD), (1, N))) / 2
        mD *= self.hydrophobic_RMST_gamma

        E_criterion = np.greater(LLink + mD, D).astype(int)
        E_final = np.multiply(E_criterion, D)

        nonzeros = np.nonzero(np.triu(E_final))
        matches = list(
            zip(
                (node_dictionary[key] for key in nonzeros[0].tolist()),
                (node_dictionary[key] for key in nonzeros[1].tolist()),
            )
        )

        print(
            "    RMST sparsification used. Accepted: "
            + str(len(matches) - np.count_nonzero(np.triu(Emst)))
            + ", rejected: "
            + str(len(graph.edges) - len(matches))
            + "; MST size: "
            + str(np.count_nonzero(np.triu(Emst)))
        )

        return matches

    def _RMST_prim_algorithm(self, D):
        """
        A very fast implementation of the first half of the RMST algorithm.
        Credit:
        """

        # Initialise matrix containing all largest links along MST
        LLink = -10 * np.ones(np.shape(D))

        # Number of nodes in the network
        N = np.shape(D)[0]

        # Allocate a matrix for the edge list
        E = np.zeros(np.shape(D))

        # Start with a node
        mstidx = np.full(shape=1, fill_value=0)
        otheridx = np.arange(1, N, 1)

        T = D[[0], [otheridx]]
        P = np.zeros((np.size(T)))

        while T.size > 0:
            i = np.argmin(T)
            idx = otheridx[i]

            # Start with a node
            E[idx, int(P[i])] = D[idx, int(P[i])]
            E[int(P[i]), idx] = D[int(P[i]), idx]

            # 1) Update the longest links
            # indexes of the nodes without the parent
            idxremove = np.where(P[i] == mstidx)
            tempmstidx = mstidx
            tempmstidx = np.delete(tempmstidx, idxremove)

            # 2) update the link to the parent
            LLink[idx, int(P[i])] = D[idx, int(P[i])]
            LLink[int(P[i]), idx] = D[int(P[i]), idx]

            # 3) find the maximal
            tempLLink = np.maximum(LLink[int(P[i]), tempmstidx], D[idx, int(P[i])])
            LLink[idx, tempmstidx] = tempLLink
            LLink[tempmstidx, idx] = tempLLink

            # As a node is added clear his entries
            P = np.delete(P, i)
            T = np.delete(T, i, 1)
            # Add the node to the list
            mstidx = np.append(mstidx, idx)

            # Remove the node from the list of the free nodes
            otheridx = np.delete(otheridx, i)

            # update the distance matrix
            Ttemp = D[[idx], [otheridx]]
            if len(T) > 0:
                idxless = (Ttemp < T).nonzero()
                T[idxless] = Ttemp[idxless]
                P[
                    idxless[1]
                ] = idx  # [1] is necessary since np.where seems to always consider two dimensions

        return E, LLink

    def hydrophobic_potential(self, r):
        """Hydrophobic potential as defined in
        Lin, M. S., Fawzi, N. L. & Head-Gordon, T.
        Hydrophobic Potential of Mean Force as a Solvation Function for Protein Structure Prediction. Structure 15, 727–740 (2007).
        """

        c = np.array([3.81679, 5.46692, 7.11677])
        w = np.array([1.68589, 1.39064, 1.57417])
        h = np.array([-0.73080, 0.20016, -0.09055])
        presum = h * np.exp(-(((r - c) / w) ** 2))

        return np.sum(presum)

    def only_bonded_to_CSH(self, atom):
        """Check whether atom is only bonded to Carbon, Sulfur or Hydrogen"""
        if self.hydrophobic_neighbourhood == 1:
            for nhbr in self.covalent_bonds_graph.neighbors(atom.id):
                if self.protein.atoms[nhbr].element not in ["C", "S", "H"]:
                    return False
            return True

        elif self.hydrophobic_neighbourhood == 2:
            first_and_second_nbhd = set(
                sec_neighborhood(self.covalent_bonds_graph, atom.id)
            ).union(set(nhbr for nhbr in self.covalent_bonds_graph.neighbors(atom.id)))
            for item in first_and_second_nbhd:
                if self.protein.atoms[item].element not in ["C", "S", "H"]:
                    return False
            return True

        else:
            raise GraphConstructionError(
                "The extent for the neighbourhood variable needs to be either 1 or 2. "
            )

    ########################
    # STACKED INTERACTIONS #
    ########################

    def find_stacked(self):
        """Function that implements pi stacking interactions, with inspiration taken from Antoine Delmotte's DNA code.
        No input, function will add bonds to internal bond list

        Florian Song, May 2017
        """
        print("Finding stacked interactions in DNA...")

        # Parameters for DNA stacking
        energy_threshold = (
            0.0019872041 * 300  # * (self.k_factor * 4.184 / 6.022)
        )  # Energy of thermal fluctuations at temperature 300 K
        A = 0.214
        C = 4.7e4
        alpha = 12.35
        epsilon = 4.0

        def get_atom_specific_parameters(atom_name):
            """
            Function to get atom-specific parameters, in particular K (???) & R (vdW radii)
            input: atom name
            output: tuple of relevant K and R
            """
            K = (1.0, 1.0, 1.0, 1.18, 1.36)  # Respectively for H, C, C_arom, N and O
            R = (1.20, 1.70, 1.77, 1.6, 1.5)  # Respectively for H, C, C_arom, N and O

            atom_name = atom_name.strip()
            if atom_name[0] == "H":
                element_identifier = 0
            elif atom_name[0] == "C":
                if atom_name == "C7":
                    element_identifier = 1
                else:
                    element_identifier = 2
            elif atom_name[0] == "N":
                element_identifier = 3
            elif atom_name[0] == "O":
                element_identifier = 4
            else:
                raise GraphConstructionError(
                    "Could not find atom: "
                    + atom_name
                    + " for atom-specific parameters in stacked interactions. "
                )
            return K[element_identifier], R[element_identifier]

        def get_pisigma_charges(base, normal, coord, base_atom_name):
            """
            Function to get sigma and pi charges from data in parameters.py
            input:
            base = one of DA, DC, DG, DT
            normal = np vector in 3d, the normal to the ring plane
            coord = np vector in 3d, coordinates of the relevant atom
            base_atom_name = name of relevant atom, eg. "C2"
            """

            # retrieve the relevant dictionary from parameters.py
            dictio = {
                "DA": bagpype.parameters.DA_sigma_pi,
                "DC": bagpype.parameters.DC_sigma_pi,
                "DT": bagpype.parameters.DT_sigma_pi,
                "DG": bagpype.parameters.DG_sigma_pi,
            }[base]

            # retrieve charges and build list: [pi, sigma, pi]
            atom_charges = dictio[base_atom_name]
            q = [atom_charges[1]]
            q.extend(atom_charges)

            # list of coordinates for point charges
            above = coord + 0.47 * normal
            below = coord - 0.47 * normal
            c = [above, coord, below]

            return q, c

        # lists of indices for all atoms in the nucleobase (on the basis of which the energies are calculated)
        # as well as indices for the rings only
        dna_atom_inds = {}
        dna_atom_ring = {}
        for atom in self.protein.atoms:
            if (
                atom.res_name in ["DT", "DC", "DA", "DG"]
                and "'" not in atom.name
                and "P" not in atom.name
            ):
                dna_atom_inds.setdefault(
                    (atom.chain, int(atom.res_num), atom.res_name), []
                ).append(atom.id)
                if atom.name in ["C2", "C4", "C5", "C6", "N1", "N3"]:
                    dna_atom_ring.setdefault(
                        (atom.chain, int(atom.res_num), atom.res_name), {}
                    ).update({atom.name: atom.id})

        close_DNA_residues = []

        def determine_if_DNA_residues_are_linked(set1, set2):
            for n1 in set1:
                for n2 in self.possible_bonds[n1]:
                    if n2 in set2:
                        return True
            return False

        set_nuclear_bases = dna_atom_inds.keys()
        for pair_of_bases in itertools.combinations(set_nuclear_bases, 2):
            base1, base2 = pair_of_bases
            set_of_atoms1, set_of_atoms2 = (
                set(dna_atom_inds[base1]),
                set(dna_atom_inds[base2]),
            )

            if determine_if_DNA_residues_are_linked(set_of_atoms1, set_of_atoms2):
                close_DNA_residues.append(pair_of_bases)

        # Initialisations
        sum_vdw_electro = {}

        # ENERGY CALCULATIONS:
        # for each pair of nucleobases calculate the energies between them using every single pair of atoms
        # formulae for these are in Antoine's thesis
        for pair_i in close_DNA_residues:

            base_key1, base_key2 = pair_i

            # retrieve list of atoms in particular base, get the name of base and compute the normal to the plane
            id_list1 = dna_atom_inds[base_key1]
            base1 = base_key1[2]
            normal1 = np.cross(
                self.protein.atoms[id_list1[0]].xyz - self.protein.atoms[id_list1[1]].xyz,
                self.protein.atoms[id_list1[0]].xyz - self.protein.atoms[id_list1[2]].xyz,
            )
            normal1 = normal1 / np.linalg.norm(normal1)

            id_list2 = dna_atom_inds[base_key2]
            base2 = base_key2[2]
            normal2 = np.cross(
                self.protein.atoms[id_list2[0]].xyz - self.protein.atoms[id_list2[1]].xyz,
                self.protein.atoms[id_list2[0]].xyz - self.protein.atoms[id_list2[2]].xyz,
            )
            normal2 = normal2 / np.linalg.norm(normal2)

            # compute van der Waals component for every pair of atoms between both nucleobases
            vdw = 0
            for id1 in id_list1:
                K1, R1 = get_atom_specific_parameters(self.protein.atoms[id1].name)

                for id2 in id_list2:
                    K2, R2 = get_atom_specific_parameters(self.protein.atoms[id2].name)

                    z = np.linalg.norm(
                            self.protein.atoms[id1].xyz - self.protein.atoms[id2].xyz
                        ) / (2.0 * np.sqrt(R1 * R2))
                    vdw += (
                        # (-self.k_factor * 4.184 / 6.022)
                        -1
                        * K1
                        * K2
                        * (C * np.exp(-alpha * z) - A / z ** 6.0)
                    )

            # do the same with the electrostatic component, including two 3x loops for the 3 point charges
            electro = 0
            for id1 in id_list1:
                q1, c1 = get_pisigma_charges(
                    base1, normal1, self.protein.atoms[id1].xyz, self.protein.atoms[id1].name
                )

                for id2 in id_list2:
                    q2, c2 = get_pisigma_charges(
                        base2,
                        normal2,
                        self.protein.atoms[id2].xyz,
                        self.protein.atoms[id2].name,
                    )

                    for m in range(3):
                        for n in range(3):
                            electro += (
                                # (-self.k_factor * 4.184 / 6.022)
                                -1
                                * (332 / epsilon)
                                * q1[m]
                                * q2[n]
                                / (np.linalg.norm(c1[m] - c2[n]))
                            )
            sum_vdw_electro[(base_key1, base_key2)] = vdw + electro

        # sum both energies and only keep those that surpass the threshold
        for key in list(sum_vdw_electro.keys()):
            if not sum_vdw_electro[key] > energy_threshold:
                sum_vdw_electro.pop(key)

        # spread this energy onto the 6 corresponding atoms of the rings
        for key in sorted(
            sum_vdw_electro, key=lambda x: (x[0][0], x[0][1], x[1][0], x[1][1])
        ):
            base_key1, base_key2 = key[0], key[1]

            edge_dic1, edge_dic2 = dna_atom_ring[base_key1], dna_atom_ring[base_key2]

            for ring_atom in ["C2", "C4", "C5", "C6", "N1", "N3"]:
                bond_strength = sum_vdw_electro[key] / 6.0

                atom1_id, atom2_id = edge_dic1[ring_atom], edge_dic2[ring_atom]
                atom1, atom2 = self.protein.atoms[atom1_id], self.protein.atoms[atom2_id]

                self.bonds.append(
                    bagpype.molecules.Bond([], atom1, atom2, bond_strength, "STACKED")
                )

    #############################
    # DNA BACKBONE INTERACTIONS #
    #############################

    def find_DNA_backbone(self):
        """Function that implements interactions between phosphates in DNA as per Antoine Delmotte's DNA code.
        No input, function will add bonds to internal bond list
        Florian, May 2017
        """
        print("Finding DNA backbone interactions...")

        # Various parameters
        delta = 0.24  # 0.1522
        epsilon = 4  # 80.
        concentration = 0.1  # Mol/l
        debye = 1 / (0.329 * (concentration ** 0.5))

        # get a list of all relevant atoms - all "OP1" atoms
        OP1_atoms = []
        for atom in self.protein.atoms:
            if atom.res_name in ["DT", "DC", "DA", "DG"] and atom.name == "OP1":
                OP1_atoms.append(atom.id)

        # For each pair of OP1 atoms, compute the interaction energy
        # (This is so inefficient but doesn't make too much of a difference in the grand scheme of things
        # -> think about more efficient implementation if DNA is huge)
        for i in range(len(OP1_atoms) - 1):
            for j in range(i, len(OP1_atoms)):
                atom1_id = OP1_atoms[i]
                atom2_id = OP1_atoms[j]

                # Consider only OP1's that are in the same chain and within one residue of each other
                if self.protein.atoms[atom1_id].chain == self.protein.atoms[
                    atom2_id
                ].chain and (
                    int(self.protein.atoms[atom1_id].res_num)
                    == int(self.protein.atoms[atom2_id].res_num) + 1
                    or int(self.protein.atoms[atom1_id].res_num)
                    == int(self.protein.atoms[atom2_id].res_num) - 1
                ):

                    # compute the bond energy using the formula stated in Antoine's thesis
                    rij = np.linalg.norm(
                        self.protein.atoms[atom1_id].xyz - self.protein.atoms[atom2_id].xyz
                    )
                    energy = abs(
                        # (-self.k_factor * 4.184 / 6.022)
                        -1
                        * (332 / epsilon)
                        * (delta ** 2)
                        / rij
                    ) * np.exp(-1 * rij / debye)

                    # add this to the bond dictionary
                    atom1 = self.protein.atoms[atom1_id]
                    atom2 = self.protein.atoms[atom2_id]
                    self.bonds.append(
                        bagpype.molecules.Bond([], atom1, atom2, energy, "BACKBONE")
                    )

    ##############################
    # ELECTROSTATIC INTERACTIONS #
    ##############################

    def find_electrostatics(self):
        """
        Load electrostatic bonds using LINK entries in the PDB file.
        Some of these may be covalent bonds though. These are added as well,
        but in a different sub-routine.
        """

        if self.protein.LINKs:
            print("Finding electrostatic interactions using LINK entries...")

            # epsilon is the dielectric constant
            epsilon = 4

            for LINK_entry in self._LINK_list:
                if LINK_entry[1]["is_covalent"]:
                    # For this case, see find_covalent().
                    pass
                else:
                    # Load the two atoms from the LINK entry parsed in parsing.
                    atom1, atom2 = LINK_entry[0]

                    # The bond distance given in the LINK entry is not always accurate.
                    bond_length = distance_between_two_atoms(atom1, atom2)

                    if bond_length > 10:
                        print(
                            (
                                "WARNING: The LINK entry between {} and {} has a distance value greater than 10A. "
                                "It will be skipped. A possible reason for this is due to multimer replication."
                            ).format(atom1, atom2)
                        )
                        continue

                    if LINK_entry[1]["distance"] != round(bond_length, 2):
                        print(
                            "WARNING: The LINK entry between {} and {} has a different distance value than the computed one:"
                            " Computed = {}, in PDB = {}. The computed one will be used.".format(
                                atom1,
                                atom2,
                                round(bond_length, 2),
                                LINK_entry[1]["distance"],
                            )
                        )

                    try:
                        # Find charges of the two atoms using charge database
                        # in bagpype.parameters file
                        charge1 = bagpype.parameters.charges[atom1.res_name][atom1.name]
                        charge2 = bagpype.parameters.charges[atom2.res_name][atom2.name]

                        # Calculate bond strength using Coulomb's law
                        bond_strength = (332 * charge1 * charge2) / (epsilon * bond_length)
                        # bond_strength *= -self.k_factor * 4.184 / 6.022
                        bond_strength *= -1
                        if bond_strength > 0:
                            bond = bagpype.molecules.Bond(
                                [], atom1, atom2, bond_strength, "ELECTROSTATIC"
                            )
                            self.bonds.append(bond)
                    except (KeyError):
                        print(
                            "WARNING: Charge of atom {0} from residue {1} ".format(
                                atom1.name, atom1.res_name
                            )
                            + "or atom {0} from residue {1} ".format(
                                atom2.name, atom2.res_name
                            )
                            + "could not be found in the charge database, please update. Ignoring this LINK entry for now."
                        )


# Functions that are used across different bond types and generally a lot throughout the script


def in_same_residue(atom1, atom2):
    return (
        atom1.res_num == atom2.res_num
        and atom1.chain == atom2.chain
        and atom1.res_name == atom2.res_name
    )


def distance_between_two_atoms(atom1, atom2):
    return np.round(np.linalg.norm(atom1.xyz - atom2.xyz), capping_decimals)


def equilibrium_distance(atom1, atom2):
    return (
        bagpype.parameters.covalent_radii[atom1.element]
        + bagpype.parameters.covalent_radii[atom2.element]
    )


def sec_neighborhood(G, node):
    """Returns the second neighbourhood of a node in a Graph G as a set;
    does not include the node itself
    """
    sec_nbhood = set([node])
    for n in G[node]:
        sec_nbhood.update(G.neighbors(n))
    sec_nbhood.remove(node)
    return list(sec_nbhood)


def within_cov_bonding_distance(atom1, atom2, tolerance=0.1):
    """Checks distance between two atoms is within covalent
    bonding distance for that pair of elements.  Uses the
    parameters specified by Pyykkö (doi: 10.1002/chem.200901472).
    """
    try:
        cutoff = bagpype.parameters.distance_cutoffs[(atom1.element, atom2.element)]
    except KeyError:
        try:
            cutoff = bagpype.parameters.distance_cutoffs[(atom2.element, atom1.element)]
        except KeyError:
            cutoff = (
                bagpype.parameters.covalent_radii[atom1.element]
                + bagpype.parameters.covalent_radii[atom2.element]
            )
            cutoff += tolerance

    return (
        True
        if cutoff is not None and distance_between_two_atoms(atom1, atom2) < min(cutoff, 3.0)
        else False
    )


def in_third_neighbourhood(G, source_atom, target_atom):
    """check if a node is within distance at most d=3 from another node"""
    # length = nx.single_source_shortest_path_length(G, source_atom.id, cutoff=3)
    # return True if target_atom.id in length else False
    for edge in nx.bfs_edges(G, source=source_atom.id, depth_limit=3):
        if target_atom.id in edge:
            return True
    return False


def uniquify(bond_list):
    """Takes a list of bonds which may contain multiple bonds between
    the same two atoms and returns a list where these have been combined
    into a single bond with the interaction energies summed.

    Parameters
    ----------
    bond_list : BondList
      List of non-unique bonds

    Returns
    -------
    bonds_unique : BondList
      List of bonds where separate Bonds between the same two atoms are
      combined into a single Bond with the interaction energies summed
    """

    bonds_unique = bagpype.molecules.BondList()
    count = 0
    seen = {}
    for bond in bond_list:
        try:
            bonds_unique[seen[bond]].add_interaction(bond.weight, bond.bond_type)
        except:
            seen[bond] = count * 1
            bonds_unique.append(bond)
            count += 1
    return bonds_unique
