#!/usr/bin/env python
import argparse
from concat_with_softmax import ConcatWithSoftmax
import itertools
import json
import numpy as np
from simple_rnn import SimpleRnn
from sklearn.metrics import confusion_matrix
from stats import Stats
import sys
from tied_embeddings import TiedEmbeddings
import time
import theano
import theano.tensor as T
import os
import util
from updates import vanilla, rmsprop
from vocab import Vocab

parser = argparse.ArgumentParser()
parser.add_argument("--train-set", default="data/snli_1.0_train.jsonl")
parser.add_argument("--num-from-train", default=-1, type=int, help='number of egs to read from train. -1 => all')
parser.add_argument("--dev-set", default="data/snli_1.0_dev.jsonl")
parser.add_argument("--num-from-dev", default=-1, type=int, help='number of egs to read from dev. -1 => all')
parser.add_argument("--dev-run-freq", default=100000, type=int, help='frequency (in num examples trained) to run against dev set')
parser.add_argument("--num-epochs", default=-1, type=int, help='number of epoches to run. -1 => forever')
parser.add_argument("--max-run-time-sec", default=-1, type=int, help='max secs to run before early stopping. -1 => dont early stop')
parser.add_argument('--learning-rate', default=0.01, type=float, help='learning rate')
parser.add_argument('--update-fn', default='vanilla', help='vanilla (sgd) or rmsprop')
parser.add_argument('--embedding-dim', default=100, type=int, help='embedding node dimensionality')
parser.add_argument('--hidden-dim', default=50, type=int, help='hidden node dimensionality')
parser.add_argument('--bidirectional', action='store_true', help='whether to build bidirectional rnns for s1 & s2')
parser.add_argument('--tied-embeddings', action='store_true', help='whether to tie embeddings for each RNN')
parser.add_argument('--l2-penalty', default=0.0001, type=float, help='l2 penalty for params')
opts = parser.parse_args()
print >>sys.stderr, opts

NUM_LABELS = 3

def log(s):
    print >>sys.stderr, util.dts(), s

# slurp training data, including converting of tokens -> ids
vocab = Vocab()
train_x, train_y, train_stats = util.load_data(opts.train_set, vocab,
                                               update_vocab=True,
                                               max_egs=int(opts.num_from_train))
log("train_stats %s %s" % (len(train_x), train_stats))
dev_x, dev_y, dev_stats = util.load_data(opts.dev_set, vocab,
                                         update_vocab=False,
                                         max_egs=int(opts.num_from_dev))
log("dev_stats %s %s" % (len(dev_x), dev_stats))

# input/output vars
s1_idxs = T.ivector('s1')  # sequence for sentence one
s2_idxs = T.ivector('s2')  # sequence for sentence two
actual_y = T.ivector('y')  # single for sentence pair label; 0, 1 or 2


# keep track of different "layers" that handle their own gradients.
# includes rnns, final concat & softmax and, potentially, special handling for
# tied embeddings
layers = []

# helper to build an rnn across s1 or s2.
# bidirectional passes are made with explicitly reversed idxs
def rnn(idxs=None, sequence_embeddings=None):
    return SimpleRnn(vocab.size(), opts.embedding_dim, opts.hidden_dim, opts.update_fn,
                     idxs=idxs, sequence_embeddings=sequence_embeddings)
rnns = None

# decide set of sequence idxs we'll be processing. there will always the two
# for the forward passes over s1 and s2 and, optionally, two more for the
# reverse pass over s1 & s2 in the bidirectional case.
idxs = [s1_idxs, s2_idxs]
if opts.bidirectional:
    idxs.extend([s1_idxs[::-1], s2_idxs[::-1]])

# build rnns. we know we will build an rnn for each sequence idx but depending
# on whether we are using tied embeddings there will be either 1 global embedding matrix
# (whose gradients are managed by TiedEmbeddings) or there will be 1 embeddings matrix per
# rnn (whose gradients are managed by the rnn itself)
if opts.tied_embeddings:
    # make shared tied embeddings helper
    tied_embeddings = TiedEmbeddings(vocab.size(), opts.embedding_dim)
    layers.append(tied_embeddings)
    # build an rnn per idx slices. rnn don't maintain their own embeddings in this case.
    slices = tied_embeddings.slices_for_idxs(idxs)
    rnns = [rnn(sequence_embeddings=s) for s in slices]
else:
    # no tied embeddings; each rnn handles it's own weights
    rnns = [rnn(idxs=i) for i in idxs]
layers.extend(rnns)

# concat final states of rnns, do a final linear combo and apply softmax for prediction.
h0 = theano.shared(np.zeros(opts.hidden_dim, dtype='float32'), name='h0', borrow=True)
final_rnn_states = [rnn.final_state_given(h0) for rnn in rnns]
concat_with_softmax = ConcatWithSoftmax(final_rnn_states, NUM_LABELS, opts.hidden_dim, opts.update_fn)
layers.append(concat_with_softmax)
prob_y, pred_y = concat_with_softmax.prob_pred()

# calc l2_sum across all params
params = [l.params_for_l2_penalty() for l in layers]
l2_sum = sum([(p**2).sum() for p in itertools.chain(*params)])

# calculate cost ; xent + l2 penalty
cross_entropy_cost = T.mean(T.nnet.categorical_crossentropy(prob_y, actual_y))
l2_cost = opts.l2_penalty * l2_sum
total_cost = cross_entropy_cost + l2_cost

#TODO: a debug hook for norms too

# calculate updates
updates = []
for layer in layers:
    updates.extend(layer.updates_wrt_cost(total_cost, opts.learning_rate))

log("compiling")
train_fn = theano.function(inputs=[s1_idxs, s2_idxs, actual_y],
                           outputs=[total_cost],
                           updates=updates)
test_fn = theano.function(inputs=[s1_idxs, s2_idxs, actual_y],
                          outputs=[pred_y, total_cost])

def stats_from_dev_set(stats):
    actuals = []
    predicteds  = []
    for (s1, s2), y in zip(dev_x, dev_y):
        pred_y, cost = test_fn(s1, s2, [y])
        actuals.append(y)
        predicteds.append(pred_y)
        stats.record_dev_cost(cost)
    dev_c = confusion_matrix(actuals, predicteds)
    dev_accuracy = util.accuracy(dev_c)
    stats.set_dev_accuracy(dev_accuracy)
    print "dev confusion\n %s (%s)" % (dev_c, dev_accuracy)


log("training")
epoch = 0
training_early_stop_time = opts.max_run_time_sec + time.time()
stats = Stats(opts)
while epoch != opts.num_epochs:
    for (s1, s2), y in zip(train_x, train_y):
        cost, = train_fn(s1, s2, [y])
        stats.record_training_cost(cost)
        early_stop = False
        if opts.max_run_time_sec != -1 and time.time() > training_early_stop_time:
            early_stop = True
        if stats.n_egs_trained % opts.dev_run_freq == 0 or early_stop:
            stats_from_dev_set(stats)
            stats.flush_to_stdout(epoch)
        if early_stop:
            exit(0)
    epoch += 1
