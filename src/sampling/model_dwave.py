"""
Class for estimating the model distribution of an RBM with dwave quantum annealing
"""

import json

import numpy as np
from networkx import Graph

from dwave.embedding.chimera import find_biclique_embedding
from dwave.embedding import embed_ising
from dwave_networkx import chimera_graph
from dimod.reference.samplers import SimulatedAnnealingSampler
from dwave.inspector import show
from dwave.system import DWaveSampler, FixedEmbeddingComposite

from braket.ocean_plugin import BraketDWaveSampler

from sampling.model import Model

from sampling.utils import get_destination_folder

class ModelDWave(Model):
    """
    Base class for model for DWave machines with various layouts
    """
    def __init__(self, layout, source = "dwave", beta=1.0, num_reads = 100, s_pause = 0.5):
        super(ModelDWave, self).__init__("model_dwave")

        if layout not in ['chimera', 'pegasus']:
            raise Exception('Layout not a valid one (0)'.format(layout))

        self.aws_str = "arn:aws:braket:::device/qpu/d-wave/Advantage_system4"

        self.layout = layout
        self.source = source
        self.sampler = self.create_sampler()
        self.beta = beta
        self.num_reads = num_reads
        self.s_pause = s_pause

        # Multiple passes defaults to True
        self.multiple_passes = True

        self.embeddings = {
            'aws': {
                128: {
                    1: 'sampling/models/aws_mappings/aws_parallel_mapping_128_1.json'
                },
                98: {
                    1: 'sampling/models/aws_mappings/aws_parallel_mapping_98_1.json',
                },
                64: {
                    1: 'sampling/models/aws_mappings/aws_parallel_mapping_64_1.json',
                    2: 'sampling/models/aws_mappings/aws_parallel_mapping_64_2.json',
                    3: 'sampling/models/aws_mappings/aws_parallel_mapping_64_3.json',
                    4: 'sampling/models/aws_mappings/aws_parallel_mapping_64_4.json'
                }
            },
            'dwave': {
                128: {
                    1: 'sampling/models/advantage_mappings/advantage_parallel_mapping_128_1.json'
                },
                98: {
                    1: 'sampling/models/advantage_mappings/advantage_parallel_mapping_98_1.json',
                },
                64: {
                    1: 'sampling/models/advantage_mappings/advantage_parallel_mapping_64_1.json',
                    2: 'sampling/models/advantage_mappings/advantage_parallel_mapping_64_2.json',
                    3: 'sampling/models/advantage_mappings/advantage_parallel_mapping_64_3.json',
                    4: 'sampling/models/advantage_mappings/advantage_parallel_mapping_64_4.json'
                }
            }
        }


    def set_model_parameters(self, sampler_parameters):
        """
        Set parameters for contrastive divergence
        """
        self.weights = sampler_parameters['weights']
        self.visible = sampler_parameters['visible']
        self.hidden = sampler_parameters['hidden']
        self.dataset = sampler_parameters['dataset']
        self.h_ids = sampler_parameters['h_ids']
        self.v_ids = sampler_parameters['v_ids']
        self.max_size = sampler_parameters['max_size']
        self.parallel = sampler_parameters['max_divide']

        # Test if an embedding exists for these parameters, otherwise multiple passes are required
        if self.max_size in self.embeddings[self.source].keys() and self.parallel in self.embeddings[self.source][self.max_size].keys():
            self.multiple_passes = False
        else:
            self.multiple_passes = True

    def estimate_model(self):
        """
        Estimate the model distribution by sampling quantum annealing device. Assume that the whole model or all the models will fit inside the annealer
        """
        samples = None
        embedding = self.generate_embedding()
        emb_problem = FixedEmbeddingComposite(self.sampler, embedding)

        if self.multiple_passes:
            results = []

            for pr in range(self.parallel):
                h_bias, j_coupling = self.generate_partial_couplings(pr)
                a_schedule = [
                        [0.0, 0.0],
                        [50.0, self.s_pause],
                        [1050.0, self.s_pause],
                        [1100.0, 1.0]
                        ]

                results.append(
                    emb_problem.sample_ising(
                        h_bias,
                        j_coupling,
                        return_embedding = True,
                        num_reads = self.num_reads,
                        num_spin_reversal_transforms = 5,
                        anneal_schedule = a_schedule)
                )
            samples = self.extract_values_from_partial_samples(results)

        else:
            h_bias, j_coupling = self.generate_couplings()

            a_schedule = [
                    [0.0, 0.0],
                    [50.0, self.s_pause],
                    [1050.0, self.s_pause],
                    [1100.0, 1.0]
                    ]

            response = emb_problem.sample_ising(
                h_bias,
                j_coupling,
                return_embedding = True,
                num_reads = self.num_reads,
                num_spin_reversal_transforms = 5,
                anneal_schedule = a_schedule)
            samples = self.extract_values_from_samples(response)

        return samples

    def create_sampler(self):
        """
        Initialize the sampler for the sampling process
        """
        if self.source == "dwave":
            if self.layout == "chimera":
                return DWaveSampler(solver={'topology__type': 'chimera'})
            elif self.layout == "pegasus":
                return DWaveSampler(solver={'name': 'Advantage_system4.1'})
        elif self.source == "aws":
            return BraketDWaveSampler(get_destination_folder(), device_arn = self.aws_str)
        else:
            raise Exception("Source not defined correctly!")

    def extract_values_from_partial_samples(self, response):
        """
        Take a array of multiple dwave samplesets and extract hidden and visible states from them
        """
        n_samples = len(response[0].samples())

        vis_states = np.zeros([self.parallel, n_samples, self.max_size])
        hid_states = np.zeros([self.parallel, n_samples, self.max_size])

        # Extract the values from the results object
        for pr in range(self.parallel):
            for i in range(n_samples):
                for vis_id in range(self.max_size):
                    vis_states[pr, i, vis_id] = response[pr].samples()[i]['v_0_{0}'.format(vis_id)] / 2 + 0.5

                for hid_id in range(self.max_size):
                    hid_states[pr, i, hid_id] = response[pr].samples()[i]['h_0_{0}'.format(hid_id)] / 2 + 0.5

        states = [np.zeros((n_samples, len(self.v_ids))), np.zeros((n_samples, len(self.h_ids)))]

        # Change the format of the states to correspond to the actual RBM
        for pr in range(self.parallel):
            for sample_id in range(n_samples):
                for i, v_id in enumerate(self.v_ids[pr * self.max_size: (pr + 1) * self.max_size]):
                    states[0][sample_id, v_id] = vis_states[pr, sample_id, i]

                for j, h_id in enumerate(self.h_ids[pr * self.max_size: (pr + 1) * self.max_size]):
                    states[1][sample_id, h_id] = hid_states[pr, sample_id, j]

        return states

    def extract_values_from_samples(self, response):
        """
        Take a dwave sampleset and extract hidden and visible states from them
        """
        n_samples = len(response.samples())
        vis_states = np.zeros([self.parallel, n_samples, self.max_size])
        hid_states = np.zeros([self.parallel, n_samples, self.max_size])

        # Extract the values from the results object
        for pr in range(self.parallel):
            for i in range(n_samples):
                for vis_id in range(self.max_size):
                    vis_states[pr, i, vis_id] = response.samples()[i]['v_{0}_{1}'.format(pr, vis_id)] / 2 + 0.5

                for hid_id in range(self.max_size):
                    hid_states[pr, i, hid_id] = response.samples()[i]['h_{0}_{1}'.format(pr, hid_id)] / 2 + 0.5

        states = [np.zeros((n_samples, len(self.v_ids))), np.zeros((n_samples, len(self.h_ids)))]

        # Change the format of the states to correspond to the actual RBM
        for pr in range(self.parallel):
            for sample_id in range(n_samples):
                for i, v_id in enumerate(self.v_ids[pr * self.max_size: (pr + 1) * self.max_size]):
                    states[0][sample_id, v_id] = vis_states[pr, sample_id, i]

                for j, h_id in enumerate(self.h_ids[pr * self.max_size: (pr + 1) * self.max_size]):
                    states[1][sample_id, h_id] = hid_states[pr, sample_id, j]

        return states

    def generate_embedding(self):
        """
        Generate dwave couplings for the RBM in question
        """
        if self.layout == 'chimera':
            return self.generate_chimera_embedding()
        elif self.layout == 'pegasus':
            return self.generate_pegasus_embedding()
        else:
            raise Exception('No sampler set!')

    def generate_pegasus_embedding(self):
        """
        Generate pegasus embedding from the mined embedding file
        """
        embedding = None

        try:
            if self.multiple_passes:
                source_file = self.embeddings[self.source][self.max_size][1]
            else:
                source_file = self.embeddings[self.source][self.max_size][self.parallel]
        except Exception as e:
            raise Exception("No embedding file for required parameters (Max size: {0}, parallel: {1})".format(self.max_size, self.parallel))

        with open(source_file, 'r') as advantage_file:
            embedding = json.load(advantage_file)['mapping']

        return embedding

    def generate_chimera_embedding(self):
        """
        Generate embedding for chimera layout
        """
        v_labels = []
        h_labels = []

        for i in range(self.max_size):
            v_labels.append("v_0_{0}".format(i))

        for i in range(self.max_size):
            h_labels.append("h_0_{0}".format(i))

        t_graph = self.generate_graph()
        vis, hid = find_biclique_embedding(v_labels, h_labels, 16, target_edges=t_graph.edges)

        return vis | hid

    def generate_partial_couplings(self, parallel_index):
        """
        Generate couplings for a single sub RBM inside the the RBM with a certain index
        """
        j_couplings = {}
        h_mag = {}

        for i in range(self.max_size):
            for j in range(self.max_size):
                j_couplings[('v_0_{0}'.format(i), 'h_0_{0}'.format(j))] = -self.weights[parallel_index][i, j] / 4

        for i in range(self.max_size):
            h_mag['v_0_{0}'.format(i)] = -self.visible[parallel_index][i] / 2

            for j in range(self.max_size):
                h_mag['v_0_{0}'.format(i)] -= self.weights[parallel_index][i, j] / 4

        for j in range(self.max_size):
            h_mag['h_0_{0}'.format(j)] = -self.hidden[parallel_index][j] / 2

            for i in range(self.max_size):
                h_mag['h_0_{0}'.format(j)] -= self.weights[parallel_index][i, j] / 4

        for h_key in h_mag.keys():
            h_mag[h_key] /= self.beta

        for j_key in j_couplings.keys():
            j_couplings[j_key] /= self.beta

        return h_mag, j_couplings

    def generate_couplings(self):
        """
        Generate coupling parameters h and J
        """
        j_couplings = {}
        h_mag = {}

        for pr in range(self.parallel):
            for i in range(self.max_size):
                for j in range(self.max_size):
                    j_couplings[('v_{0}_{1}'.format(pr, i), 'h_{0}_{1}'.format(pr, j))] = -self.weights[pr][i, j] / 4

            for i in range(self.max_size):
                h_mag['v_{0}_{1}'.format(pr, i)] = -self.visible[pr][i] / 2

                for j in range(self.max_size):
                    h_mag['v_{0}_{1}'.format(pr, i)] -= self.weights[pr][i, j] / 4

            for j in range(self.max_size):
                h_mag['h_{0}_{1}'.format(pr, j)] = -self.hidden[pr][j] / 2

                for i in range(self.max_size):
                    h_mag['h_{0}_{1}'.format(pr, j)] -= self.weights[pr][i, j] / 4

        for h_key in h_mag.keys():
            h_mag[h_key] /= self.beta

        for j_key in j_couplings.keys():
            j_couplings[j_key] /= self.beta

        return h_mag, j_couplings

    def generate_graph(self):
        """
        Generate the chimera graph for DWave 2000Q
        """
        with open('sampling/models/dwave2000q_6.json') as json_file:
            graph_json = json.load(json_file)

        dwave_6 = Graph()

        dwave_6.add_nodes_from(graph_json['qubits'])
        dwave_6.add_edges_from(graph_json['couplings'])

        return dwave_6

    def get_samples_num(self):
        """
        Get the amount of samples estimate_model returns
        """
        return self.num_reads

