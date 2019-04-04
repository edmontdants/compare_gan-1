# coding=utf-8
# Copyright 2018 Google LLC & Hwalsuk Lee.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Evaluation library."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
from absl import flags
from absl import logging

from compare_gan import datasets
from compare_gan import eval_utils
from compare_gan import utils

from compare_gan.metrics import save_examples as save_examples_lib

import gin
import numpy as np
from six.moves import range
import tensorflow as tf

import tensorflow_hub as hub


FLAGS = flags.FLAGS

# Special value returned when a fake image generated by a GAN has NaNs.
NAN_DETECTED = 31337.0

# DM: Giant hack. I know.
flags.DEFINE_integer(
    "force_label", None,
    "Only generate one label. 429 is baseball in Imagenet")


flags.DEFINE_integer("eval_batch_size", 64)

@gin.configurable("eval_z", blacklist=["shape", "name"])
def z_generator(shape, distribution_fn=tf.random.uniform,
                minval=-1.0, maxval=1.0, stddev=1.0, name=None):
  """Random noise distributions as TF op.

  Args:
    shape: A 1-D integer Tensor or Python array.
    distribution_fn: Function that create a Tensor. If the function has any
      of the arguments 'minval', 'maxval' or 'stddev' these are passed to it.
    minval: The lower bound on the range of random values to generate.
    maxval: The upper bound on the range of random values to generate.
    stddev: The standard deviation of a normal distribution.
    name: A name for the operation.

  Returns:
    Tensor with the given shape and dtype tf.float32.
  """
  return utils.call_with_accepted_args(
      distribution_fn, shape=shape, minval=minval, maxval=maxval,
      stddev=stddev, name=name)


def _update_bn_accumulators(sess, generated, num_accu_examples):
  """Returns True if the accumlators for batch norm were updated.

  Args:
    sess: `tf.Session` object. Checkpoint should already be loaded.
    generated: Output tensor of the generator.
    num_accu_examples: How many examples should be used to update accumulators.

  Returns:
    True if there were accumlators.
  """
  # Create update ops for batch statistic updates for each batch normalization
  # with accumlators.
  update_accu_switches = [v for v in tf.global_variables()
                          if "accu/update_accus" in v.name]
  logging.info("update_accu_switches: %s", update_accu_switches)
  if not update_accu_switches:
    return False
  sess.run([tf.assign(v, 1) for v in update_accu_switches])
  batch_size = generated.shape[0].value
  num_batches = num_accu_examples // batch_size
  for i in range(num_batches):
    if i % 500 == 0:
      logging.info("Updating BN accumulators %d/%d steps.", i, num_batches)
    sess.run(generated)
  sess.run([tf.assign(v, 0) for v in update_accu_switches])
  logging.info("Done updating BN accumulators.")
  return True


def evaluate_tfhub_module(module_spec, eval_tasks, use_tpu,
                          num_averaging_runs, step):
  """Evaluate model at given checkpoint_path.

  Args:
    module_spec: string, path to a TF hub module.
    eval_tasks: List of objects that inherit from EvalTask.
    use_tpu: Whether to use TPUs.
    num_averaging_runs: Determines how many times each metric is computed.
    step: Name of the step being evaluated

  Returns:
    Dict[Text, float] with all the computed results.

  Raises:
    NanFoundError: If generator output has any NaNs.
  """
  # Make sure that the same latent variables are used for each evaluation.
  np.random.seed(42)
  dataset = datasets.get_dataset()
  num_test_examples = dataset.eval_test_samples

  batch_size = FLAGS.eval_batch_size
  num_batches = int(np.ceil(num_test_examples / batch_size))

  # Load and update the generator.
  result_dict = {}
  fake_dsets = []
  with tf.Graph().as_default():
    tf.set_random_seed(42)
    with tf.Session() as sess:
      if use_tpu:
        sess.run(tf.contrib.tpu.initialize_system())
      def sample_from_generator():
        """Create graph for sampling images."""
        generator = hub.Module(
            module_spec,
            name="gen_module",
            tags={"gen", "bs{}".format(batch_size)})
        logging.info("Generator inputs: %s", generator.get_input_info_dict())
        z_dim = generator.get_input_info_dict()["z"].get_shape()[1].value
        z = z_generator(shape=[batch_size, z_dim])
        if "labels" in generator.get_input_info_dict():
          # Conditional GAN.
          assert dataset.num_classes

          if FLAGS.force_label is None:
            labels = tf.random.uniform(
                [batch_size], maxval=dataset.num_classes, dtype=tf.int32)
          else:
            labels = tf.constant(FLAGS.force_label, shape=[batch_size], dtype=tf.int32)

          inputs = dict(z=z, labels=labels)
        else:
          # Unconditional GAN.
          assert "labels" not in generator.get_input_info_dict()
          inputs = dict(z=z)
        return generator(inputs=inputs, as_dict=True)["generated"]
      
      if use_tpu:
        generated = tf.contrib.tpu.rewrite(sample_from_generator)
      else:
        generated = sample_from_generator()

      tf.global_variables_initializer().run()

      if _update_bn_accumulators(sess, generated, num_accu_examples=204800):
        saver = tf.train.Saver()
        save_path = os.path.join(module_spec, "model-with-accu.ckpt")
        checkpoint_path = saver.save(
            sess,
            save_path=save_path)
        logging.info("Exported generator with accumulated batch stats to "
                     "%s.", checkpoint_path)
      if not eval_tasks:
        logging.error("Task list is empty, returning.")
        return
      for i in range(num_averaging_runs):
        logging.info("Generating fake data set %d/%d.", i+1, num_averaging_runs)
        fake_dset = eval_utils.EvalDataSample(
            eval_utils.sample_fake_dataset(sess, generated, num_batches))
        fake_dsets.append(fake_dset)

        # Hacking this in here for speed for now
        save_examples_lib.SaveExamplesTask().run_after_session(fake_dset, None, step)

        logging.info("Computing inception features for generated data %d/%d.",
                     i+1, num_averaging_runs)
        activations, logits = eval_utils.inception_transform_np(
            fake_dset.images, batch_size)
        fake_dset.set_inception_features(
            activations=activations, logits=logits)
        fake_dset.set_num_examples(num_test_examples)
        if i != 0:
          # Free up some memory by releasing additional fake data samples.
          # For ImageNet128 50k images are ~9 GiB. This will blow up metrics
          # (such as fractal dimension) if num_averaging_runs > 1.
          fake_dset.discard_images()

  real_dset = eval_utils.EvalDataSample(
      eval_utils.get_real_images(
          dataset=dataset, num_examples=num_test_examples))
  logging.info("Getting Inception features for real images.")
  real_dset.activations, _ = eval_utils.inception_transform_np(
      real_dset.images, batch_size)
  real_dset.set_num_examples(num_test_examples)

  # Run all the tasks and update the result dictionary with the task statistics.
  result_dict = {}
  for task in eval_tasks:
    task_results_dicts = [
        task.run_after_session(fake_dset, real_dset)
        for fake_dset in fake_dsets
    ]
    # Average the score for each key.
    result_statistics = {}
    for key in task_results_dicts[0].keys():
      scores_for_key = np.array([d[key] for d in task_results_dicts])
      mean, std = np.mean(scores_for_key), np.std(scores_for_key)
      scores_as_string = "_".join([str(x) for x in scores_for_key])
      result_statistics[key + "_mean"] = mean
      result_statistics[key + "_std"] = std
      result_statistics[key + "_list"] = scores_as_string
    logging.info("Computed results for task %s: %s", task, result_statistics)

    result_dict.update(result_statistics)
  return result_dict
