from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import pdb, traceback, sys #
import tensorflow as tf
import numpy as np
from tqdm import tqdm as tq
import six
from collections import Counter
import os
import h5py
import json
import cPickle as pickle

### FIDDLE specific tools ###
from models import *
from io_tools import *
import visualization as viz
#############################


flags = tf.app.flags
flags.DEFINE_string('runName', 'experiment', 'Running name.')
flags.DEFINE_string('dataDir', '../data/hdf5datasets', 'Default data directory')
flags.DEFINE_string('configuration', 'configurations.json', 'configuration file [json file]')
flags.DEFINE_string('architecture', 'architecture.json', 'configuration file [json file]')
flags.DEFINE_string('restorePath', '../results/test', 'Regions to validate [bed or gff files]')
flags.DEFINE_string('visualizePrediction', 'offline', 'Prediction profiles to be plotted [online or offline] ')
flags.DEFINE_integer('savePredictionFreq', 50, 'Frequency of saving prediction profiles to be plotted')
flags.DEFINE_integer('maxEpoch', 1000, 'Number of epochs to run trainer.')
flags.DEFINE_integer('batchSize', 20, 'Batch size.')
flags.DEFINE_float('learningRate', 0.001, 'Initial learning rate.')
flags.DEFINE_float('dropout', 0.5, 'Keep probability for training dropout.')
flags.DEFINE_string('resultsDir', '../results', 'Directory for results data')
flags.DEFINE_string('inputs', 'None', 'inputs')
flags.DEFINE_string('outputs', 'None', 'outputs')
flags.DEFINE_boolean('gating', False, 'modality-wise gating to be applied at scaffold [True or False]')

FLAGS = flags.FLAGS

if FLAGS.gating:
    import pandas as pd

def main(_):

    ############################################################################
    #                           Read data to graph                             #
    ############################################################################
    # read in configurations
    with open(FLAGS.configuration, 'r') as fp:
        config = byteify(json.load(fp))

    if FLAGS.inputs is not 'None':
        print('Overwriting configurations for inputs...')
        input_ids = FLAGS.inputs.split('_')
        input_list = [key for key, val in config['Tracks'].items() if val['id'] in input_ids]
        print('Inputs', input_list)
        config['Options']['Inputs'] = input_list

    if FLAGS.outputs is not 'None':
        print('Overwriting configurations for outputs...')
        output_ids = FLAGS.outputs.split('_')
        output_list = [key for key, val in config['Tracks'].items() if val['id'] in output_ids]
        print('Outputs', output_list)
        config['Options']['Outputs'] = output_list
    # create or recognize results directory
    FLAGS.savePath = FLAGS.resultsDir + '/' + FLAGS.runName
    print('Results will be saved in ' + str(FLAGS.savePath))
    if not tf.gfile.Exists(FLAGS.savePath):
        tf.gfile.MakeDirs(FLAGS.savePath)

    # define neural network
    model = NNscaffold(config=config,
                       architecture_path=FLAGS.architecture,
                       learning_rate=FLAGS.learningRate,
                       model_path=FLAGS.savePath,
                       gating=FLAGS.gating)

    # save resulting modified architecture and configuration
    json.dump(model.architecture, open(FLAGS.savePath + "/architecture.json", 'w'))
    json.dump(model.config, open(FLAGS.savePath + "/configuration.json", 'w'))

    # read in training and validation data
    train_h5_handle  = h5py.File(os.path.join(FLAGS.dataDir, config['Options']['DataName'], 'train.h5'),'r')
    validation_h5_handle  = h5py.File(os.path.join(FLAGS.dataDir, config['Options']['DataName'], 'validation.h5'),'r')

    # create iterator over training data
    data = MultiModalData(train_h5_handle, batch_size=FLAGS.batchSize)
    batcher = data.batcher()


    to_size = min(validation_h5_handle.values()[0].shape[0], 1000)
    print('Storing validation data to the memory')
    try:
        all_keys = list(set(model.architecture['Inputs'] + model.architecture['Outputs']))
        validation_data = {key: validation_h5_handle[key][:to_size] for key in all_keys}
    except KeyError:
        print('\nERROR: Make sure that the configurations file contains the correct track names (keys), which should match the hdf5 keys\n')
        sys.exit()

    ############################################################################
    #                               Launch graph                               #
    ############################################################################

    # instantiate neural network
    model.initialize()
    model.create_monitor_variables(show_filters=False)
    model.saver()

    # instantiate training and validation log files
    header_str = 'Loss'
    for key in model.architecture['Outputs']:
        header_str += '\t' + key + '_Accuracy'
    header_str += '\n'
    with open((FLAGS.savePath + "/" + "train.txt"), "w") as train_file:
        train_file.write(header_str)
    with open((FLAGS.savePath + "/" + "validation.txt"), "w") as validation_file:
        validation_file.write(header_str)

    # select quality signals for prediction overlay during training
    num_signals = 5
    for key in model.architecture['Outputs']:
        idx = np.argsort(validation_data[key].reshape(validation_data[key].shape[0], -1).sum(axis = 1))
    idx = idx[int(validation_data[key].shape[0] / 2):(int(validation_data[key].shape[0] / 2) + num_signals)]
    input_for_prediction = {key: validation_data[key][idx] for key in model.architecture['Inputs']}
        # not sure of "orig_output" purpose here? , also, why it was pickled
    orig_output = {key: validation_data[key][idx] for key in model.architecture['Outputs']}
    pickle.dump(orig_output, open((FLAGS.savePath + "/" + 'original_outputs.pck'), "wb"))

    ############################################################################
    #                                  Train                                   #
    ############################################################################


    ######## TRAIN #########
    globalMinLoss = 1e16 # some high number

    step = 0
    train_size = train_h5_handle.values()[0].shape[0]

    print('Pre-train validation run:')
    return_dict = model.validate(validation_data, accuracy=True)
    print("Pre-train validation loss: " + str(return_dict['cost']))
    # print("Pre-train validation accuracy (%): " + str(return_dict['accuracy_' + key] / validation_data.values()[0].shape[0]))
    totalIterations = 1000


    for it in range(totalIterations):

        # Multimodal Dropout Regularizer:
        # linearly decreasing dropout probability from 20% (@ 1st iteration) to 0% (@ 1% of total iterations)
        # inputDropout = 0.2 - 0.2 * it / 10. if it <= (totalIterations // 100) else 0.
        inputDropout = 0.

        epoch = int(it * 10 * FLAGS.batchSize/train_size)

        print('\n\nEpoch: ' + str(epoch) + ', Iterations: ' + str(it))
        print('Number of examples seen: ' + str(it * 10 * FLAGS.batchSize))
        print('Input dropout probability: ' + str(inputDropout))


        t_batcher, t_trainer = 0, 0
        if FLAGS.gating:
            df = pd.DataFrame()
        for iterationNo in tq(range(10)):
            with Timer() as t:
                train_batch = batcher.next()
            t_batcher += t.secs
            with Timer() as t:
                return_dict = model.train(train_batch, accuracy=True, inp_dropout=inputDropout, batch_size=FLAGS.batchSize)
                train_summary = return_dict['summary']
                if FLAGS.gating:
                    df = df.append(get_delta_KL(return_dict, model.architecture['Inputs'], step))

            t_trainer += t.secs
            #
            if iterationNo==0:
                return_dict_train = return_dict.copy()
            else:
                # pdb.set_trace()
                return_dict_train = {key: return_dict_train[key]+val for key, val in return_dict.items()
                                     if (type(val) is np.ndarray) or (type(val) is np.float32)}
                return_dict_train.update({key: val for key, val in return_dict.items() if val is type('string')})

            step += 1

        if FLAGS.gating:
            header_tf = True if it==0 else None
            mode_tf = 'w' if it==0 else 'a'
            df.to_csv(FLAGS.savePath + '/deltaKL.txt', header=header_tf, index=None, mode=mode_tf, sep='\t')

        print('Batcher time: ' + "%.3f" % t_batcher)
        print('Trainer time: ' + "%.3f" % t_trainer)
        for key, val in return_dict_train.items():
            if type(val) is not type('some_str_type'):
                return_dict_train[key] /= iterationNo

        return_dict_train.update({key: np.sum(val) for key, val in return_dict_train.items()
                                  if (type(val) is np.ndarray) or (type(val) is np.float32)})

        return_dict_valid = model.validate(validation_data, accuracy=True)
        return_dict_valid.update({key: np.sum(val) for key, val in return_dict_valid.items()
                                  if (type(val) is np.ndarray) or (type(val) is np.float32)})
        return_dict_valid.update({key: val for key, val in return_dict_valid.items()
                                  if (type(val) is type('string'))})

        # for every 50 iteration,
        if (it % FLAGS.savePredictionFreq ==0):

            if 'dnaseq' not in model.outputs.keys():
                predicted_dict = model.predict(input_for_prediction)
                pickle.dump(predicted_dict, open((FLAGS.savePath + "/" + 'pred_viz_{}.pck'.format(it)), "wb"))
                if FLAGS.visualizePrediction == 'online':
                    viz.plot_prediction(predicted_dict, orig_output,
                                            name='iteration_{}'.format(it),
                                            save_dir=FLAGS.savePath,
                                            strand=model.config['Options']['Strand'])
            else:
                feed_d = {val: input_for_prediction[key] for key, val in model.inputs.items()}
                feed_d.update({val: orig_output[key] for key, val in model.outputs.items()})
                feed_d.update({model.dropout: 1.,
                               model.keep_prob_input: 1.,
                               model.inp_size: input_for_prediction.values()[0].shape[0],
                               K.learning_phase(): 0})
                weights, pred_vec = model.sess.run([model.dna_before_softmax, model.predictions['dnaseq']], feed_d)
                predicted_dict={'dna_before_softmax':weights,
                                'prediction': pred_vec}
                pickle.dump(predicted_dict, open((FLAGS.savePath + "/" + 'pred_viz_{}.pck'.format(it)), "wb"))
                if FLAGS.visualizePrediction == 'online':
                    viz.visualize_dna(weights, pred_vec,
                                  name='iteration_{}'.format(it),
                                  save_dir=FLAGS.savePath)
        #
        write_to_txt(return_dict_train)
        write_to_txt(return_dict_valid, batch_size=validation_data.values()[0].shape[0], case='validation')

        model.summarize( train_summary = train_summary,
                         validation_summary=return_dict_valid['summary'],
                         step=step)

        if (return_dict_valid['cost'] < globalMinLoss) and (it>20):
            globalMinLoss = return_dict_valid['cost']
            for track_name, saver in model.savers_dict.items():
                save_path = saver.save(model.sess, os.path.join(FLAGS.savePath, track_name+'_model.ckpt'))
            print('Model saved in file: %s' % FLAGS.savePath)

    model.sess.close()



def write_to_txt(return_dict, batch_size=FLAGS.batchSize, case='train', verbose=True):
    save_path = FLAGS.savePath
    line_to_write = ''
    for key, val in return_dict.items():
        if key == 'cost':
            cur_line = str(return_dict['cost'])
            line_to_write += str(return_dict[key])
        elif (key == '_') or (key == 'summary'):
            continue
        else:
            cur_line = str(return_dict[key] / batch_size)
            line_to_write += '\t' + cur_line
        if verbose:
            print(case + '\t' + key + ': ' + cur_line)

    with open((save_path + "/" + case + ".txt"), "a") as fp:
        fp.write(line_to_write + '\n')

def get_delta_KL(return_dict, track_list, iter_no):
    column_list = []
    column_list.append('Iter_no')
    column_list += track_list + ['DeltaKL']
    df = pd.DataFrame(columns=column_list)
    df['DeltaKL'] = return_dict['DeltaKL']
    for track_name in track_list:
        df[track_name] = return_dict[track_name + '_gates']
    df['Iter_no'] = iter_no

    return df
if __name__ == '__main__':
    try:
        tf.app.run()
    except:
        type, value, tb = sys.exc_info()
        traceback.print_exc()
