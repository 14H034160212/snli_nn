import math
import numpy as np
import theano
import theano.tensor as T
import util

class Embeddings(object):
    def __init__(self, vocab_size, embedding_dim,
                 idxs=None, sequence_embeddings=None):
        assert (idxs is None) ^ (sequence_embeddings is None)
        #self.name = name

        if idxs is not None:
            # not tying weights, build our own set of embeddings
            self.Wx = util.sharedMatrix(vocab_size, embedding_dim, 'Wx',
                                        orthogonal_init=True)
            self.sequence_embeddings = self.Wx[idxs]
            self.using_shared_embeddings = False
        else:
            # using tied weights, we won't be handling the update
            self.sequence_embeddings = sequence_embeddings
            self.using_shared_embeddings = True

    def params_for_l2_penalty(self):
        if self.using_shared_embeddings:
            return []
        return [self.sequence_embeddings]

    def updates_wrt_cost(self, cost, learning_opts):
        if self.using_shared_embeddings:
            return []
        learning_rate = learning_opts.learning_rate
        gradient = util.clipped(T.grad(cost=cost, wrt=self.sequence_embeddings))
        return [(self.Wx, T.inc_subtensor(self.sequence_embeddings,
                                          -learning_rate * gradient))]

    def embeddings(self):
        return self.sequence_embeddings

class TiedEmbeddings(object):
    def __init__(self, n_in, n_embedding, initial_embeddings_file=None, train_embeddings=True):
        if not train_embeddings and initial_embeddings_file is None:
            print >>sys.stderr, "WARNING: not training embedding without initial embeddings"
        self.train_embeddings = train_embeddings
        if initial_embeddings_file:
            e = np.load(initial_embeddings_file)
            assert e.shape[0] == n_in, "vocab mismatch size? loaded=%s expected=%s" % (e.shape[0], n_in)
            # TODO code could handle this but just not wanting --embedding-dim set
            # when using init embeddings
            assert e.shape[1] == n_embedding, "dimensionality config error. loaded embeddings %s d but --embedding-dim set to %s d" % (e.shape[1], n_embedding)
            assert e.dtype == np.float32, "%s" % e.dtype
            self.shared_embeddings = util.shared(e, 'tied_embeddings')
        else:
            self.shared_embeddings = util.sharedMatrix(n_in, n_embedding,
                                                       'tied_embeddings',
                                                       orthogonal_init=True)

    def slices_for_idxs(self, idxs):  # list of vectors (idxs)
        # concat all idx sequences into one sequence so we can slice into shared
        # embeddings with a _single_ operation. we need to do this only because
        # inc_subtensor only allows for one indexing :/
        concatenated_idxs = T.concatenate(idxs)
        self.concatenated_sequence_embeddings = self.shared_embeddings[concatenated_idxs]

        # but now we have to reslice back into this to pick up the embeddings per original
        # index sequence. each of these subslices is given to a seperate rnn to run over.
        sub_slices = []
        offset = 0
        for idx in idxs:
            seq_len = idx.shape[0]
            sub_slices.append(self.concatenated_sequence_embeddings[offset :
                                                                    offset + seq_len])
            offset += seq_len
        return sub_slices

    def name(self):
        return "tied_embeddings"

    def params_for_l2_penalty(self):
        if not self.train_embeddings:
            return []
        # for l2 penalty only check the subset of the embeddings related to a specific
        # example. ie NOT the entire shared_embeddings, most of which has nothing to do
        # with each example.
        return [self.concatenated_sequence_embeddings]

    def updates_wrt_cost(self, cost, learning_opts):
        if not self.train_embeddings:
            return []
        # _one_ update for the embedding matrix; regardless of the number of rnns running
        # over subslices
        gradient = util.clipped(T.grad(cost=cost,
                                       wrt=self.concatenated_sequence_embeddings))
        learning_rate = learning_opts.learning_rate
        return [(self.shared_embeddings,
                 T.inc_subtensor(self.concatenated_sequence_embeddings,
                                 -learning_rate * gradient))]
