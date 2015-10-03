#!/usr/bin/env python
import argparse
import json
import numpy as np
from simple_rnn import SimpleRnn
from sklearn.metrics import confusion_matrix
import sys
import time
import theano
import theano.tensor as T
import util
from vocab import Vocab

parser = argparse.ArgumentParser()
parser.add_argument("--train-set", default="data/snli_1.0_train.jsonl")
parser.add_argument("--num-from-train", default=-1, type=int)
parser.add_argument("--dev-set", default="data/snli_1.0_dev.jsonl")
parser.add_argument("--num-from-dev", default=-1, type=int)
parser.add_argument("--dev-run-freq", default=1000, type=int)
parser.add_argument("--num-epochs", default=-1, type=int)
parser.add_argument('--learning-rate', default=0.05, type=float, help='learning rate')
parser.add_argument('--adaptive-learning-rate-fn', default='vanilla', help='vanilla (sgd) or rmsprop')
parser.add_argument('--embedding-dim', default=3, type=int, help='embedding node dimensionality')
parser.add_argument('--hidden-dim', default=4, type=int, help='hidden node dimensionality')
opts = parser.parse_args()
print >>sys.stderr, opts

NUM_LABELS = 3

# slurp training data, including converting of tokens -> ids
print >>sys.stderr, ">loading"
vocab = Vocab()
train_x, train_y, train_stats = util.load_data(opts.train_set, vocab,
                                               update_vocab=True,
                                               max_egs=int(opts.num_from_train))
print >>sys.stderr, "train_stats", len(train_x), train_stats
dev_x, dev_y, dev_stats = util.load_data(opts.dev_set, vocab,
                                         update_vocab=False,
                                         max_egs=int(opts.num_from_dev))
print >>sys.stderr, "dev_stats", len(dev_x), dev_stats
print >>sys.stderr, "<loaded"

# input/output vars
s1_idxs = T.ivector('s1')  # sequence for sentence one
s2_idxs = T.ivector('s2')  # sequence for sentence two
actual_y = T.ivector('y')  # single for sentence pair label; 0, 1 or 2

# shared initial zero hidden state
h0 = theano.shared(np.zeros(opts.hidden_dim, dtype='float32'), name='h0', borrow=True)

# build rnn for pass over s1
config = (vocab.size(), opts.embedding_dim, opts.hidden_dim, True)
s1_rnn = SimpleRnn(*config)
final_s1_state = s1_rnn.final_state_given(s1_idxs, h0)

# build another rnn for pass over s2
s2_rnn = SimpleRnn(*config)
final_s2_state = s2_rnn.final_state_given(s2_idxs, h0)

# concat, do a final linear combo and apply softmax
concatted_state = T.concatenate([final_s1_state, final_s2_state])
Wy = util.sharedMatrix(NUM_LABELS, 2 * opts.hidden_dim, 'Wy', False)
by = util.shared(util.zeros((1, NUM_LABELS)), 'by')
prob_y = T.nnet.softmax(T.dot(Wy, concatted_state) + by)
pred_y = T.argmax(prob_y, axis=1)

cross_entropy = T.mean(T.nnet.categorical_crossentropy(prob_y, actual_y))

model_params = s1_rnn.params() + s2_rnn.params() + [Wy, by]

gradients = T.grad(cost=cross_entropy, wrt=model_params)

def vanilla(params, gradients):
    return [(param, param - opts.learning_rate * gradient) for param, gradient in zip(params, gradients)]

def rmsprop(params, gradients):
    updates = []
    for param_t0, gradient in zip(params, gradients):
        # rmsprop see slide 29 of http://www.cs.toronto.edu/~tijmen/csc321/slides/lecture_slides_lec6.pdf
        # first the mean_sqr exponential moving average
        mean_sqr_t0 = theano.shared(np.zeros(param_t0.get_value().shape, dtype=param_t0.get_value().dtype))  # zeros in same shape are param
        mean_sqr_t1 = 0.9 * mean_sqr_t0 + 0.1 * gradient**2
        updates.append((mean_sqr_t0, mean_sqr_t1))
        # update param surpressing gradient by this average
        param_t1 = param_t0 - opts.learning_rate * (gradient / T.sqrt(mean_sqr_t1 + 1e-10))
        updates.append((param_t0, param_t1))
    return updates

update_fn = globals().get(opts.adaptive_learning_rate_fn)
updates = update_fn(model_params, gradients)

print >>sys.stderr, ">compiling"
train_fn = theano.function(inputs=[s1_idxs, s2_idxs, actual_y],
                           outputs=[],
                           updates=updates)
test_fn = theano.function(inputs=[s1_idxs, s2_idxs],
                          outputs=[pred_y])
print >>sys.stderr, "<compiling"

def test_on_dev_set():
    actuals = []
    predicteds  = []
    for n, ((s1, s2), y) in enumerate(zip(dev_x, dev_y)):
        pred_y, = test_fn(s1, s2)
        actuals.append(y)
        predicteds.append(pred_y)
    dev_c = confusion_matrix(actuals, predicteds)
    dev_c_accuracy = util.accuracy(dev_c)
    print "dev confusion\n %s (%s)" % (dev_c, dev_c_accuracy)
    s1_wb_norm = np.linalg.norm(s1_rnn.Wb.get_value())
    s2_wb_norm = np.linalg.norm(s2_rnn.Wb.get_value())
    print "s1.Wb", s1_rnn.Wb.get_value()
    print "s2.Wb", s2_rnn.Wb.get_value()
    print "STATS\t%s" % "\t".join(map(str, [dev_c_accuracy, s1_wb_norm, s2_wb_norm]))
    sys.stdout.flush()

epoch = 0
n_egs_since_dev_test = 0
while epoch != opts.num_epochs:
    print ">epoch %s (%s)" % (epoch, time.strftime("%Y-%m-%d %H:%M:%S"))
    for (s1, s2), y in zip(train_x, train_y):
        train_fn(s1, s2, [y])
        n_egs_since_dev_test += 1
        if n_egs_since_dev_test == opts.dev_run_freq:
            test_on_dev_set()
            n_egs_since_dev_test = 0
    epoch += 1
