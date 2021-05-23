"""
The training and testing loop.
"""

import logging
import multiprocessing as mp
import os

import tensorflow as tf
import wandb

import models.registry as models_registry
from plato.config import Config
from plato.trainers import base


class Trainer(base.Trainer):
    """A basic federated learning trainer for TensorFlow, used by both
    the client and the server.
    """
    def __init__(self, client_id=0, model=None):
        """Initializing the trainer with the provided model.

        Arguments:
        client_id: The ID of the client using this trainer (optional).
        model: The model to train.
        """
        super().__init__(client_id)

        if model is None:
            self.model = models_registry.get()

    def zeros(self, shape):
        """Returns a TensorFlow zero tensor with the given shape."""
        # This should only be called from a server
        assert self.client_id == 0
        return tf.zeros(shape)

    def save_model(self, filename=None):
        """Saving the model to a file."""
        model_name = Config().trainer.model_name
        model_dir = Config().params['model_dir']

        if not os.path.exists(model_dir):
            os.makedirs(model_dir)

        if filename is not None:
            model_path = f'{model_dir}{filename}'
        else:
            model_path = f'{model_dir}{model_name}.ckpt'

        self.model.save_weights(model_path)

        if self.client_id == 0:
            logging.info("[Server #%d] Model saved to %s.", os.getpid(),
                         model_path)
        else:
            logging.info("[Client #%d] Model saved to %s.", self.client_id,
                         model_path)

    def load_model(self, filename=None):
        """Loading pre-trained model weights from a file."""
        model_name = Config().trainer.model_name
        model_dir = Config().params['model_dir']

        if filename is not None:
            model_path = f'{model_dir}{filename}'
        else:
            model_path = f'{model_dir}{model_name}.ckpt'

        if self.client_id == 0:
            logging.info("[Server #%d] Loading a model from %s.", os.getpid(),
                         model_path)
        else:
            logging.info("[Client #%d] Loading a model from %s.",
                         self.client_id, model_path)

        self.model.load_weights(model_path)

    def train_process(self, config, trainset, sampler, cut_layer=None):
        """The main training loop in a federated learning workload, run in
          a separate process with a new CUDA context, so that CUDA memory
          can be released after the training completes.

        Arguments:
        config: a dictionary of configuration parameters.
        trainset: The training dataset.
        sampler: the sampler that extracts a partition for this client.
        cut_layer (optional): The layer which training should start from.
        """
        if hasattr(Config().trainer, 'use_wandb'):
            run = wandb.init(project="plato",
                             group=str(config['run_id']),
                             reinit=True)

        custom_train = getattr(self, "train_model", None)

        if callable(custom_train):
            self.train_model(config, trainset, sampler.get(), cut_layer)
        else:
            log_interval = 10
            batch_size = config['batch_size']

            logging.info("[Client #%d] Loading the dataset.", self.client_id)
            _train_loader = getattr(self, "train_loader", None)

            if callable(_train_loader):
                train_loader = self.train_loader(batch_size, trainset,
                                                 sampler.get(), cut_layer)
            else:
                train_loader = torch.utils.data.DataLoader(
                    dataset=trainset,
                    shuffle=False,
                    batch_size=batch_size,
                    sampler=sampler.get())

            iterations_per_epoch = np.ceil(len(trainset) /
                                           batch_size).astype(int)
            epochs = config['epochs']

            # Sending the model to the device used for training
            self.model.to(self.device)
            self.model.train()

            # Initializing the loss criterion
            _loss_criterion = getattr(self, "loss_criterion", None)
            if callable(_loss_criterion):
                loss_criterion = _loss_criterion(self.model)
            else:
                loss_criterion = nn.CrossEntropyLoss()

            # Initializing the optimizer
            get_optimizer = getattr(self, "get_optimizer",
                                    optimizers.get_optimizer)
            optimizer = get_optimizer(self.model)

            # Initializing the learning rate schedule, if necessary
            if hasattr(Config().trainer, 'lr_schedule'):
                lr_schedule = optimizers.get_lr_schedule(
                    optimizer, iterations_per_epoch, train_loader)
            else:
                lr_schedule = None

            for epoch in range(1, epochs + 1):
                for batch_id, (examples, labels) in enumerate(train_loader):
                    examples, labels = examples.to(self.device), labels.to(
                        self.device)
                    optimizer.zero_grad()

                    if cut_layer is None:
                        outputs = self.model(examples)
                    else:
                        outputs = self.model.forward_from(examples, cut_layer)

                    loss = loss_criterion(outputs, labels)

                    loss.backward()

                    optimizer.step()

                    if lr_schedule is not None:
                        lr_schedule.step()

                    if batch_id % log_interval == 0:
                        if self.client_id == 0:
                            logging.info(
                                "[Server #{}] Epoch: [{}/{}][{}/{}]\tLoss: {:.6f}"
                                .format(os.getpid(), epoch, epochs, batch_id,
                                        len(train_loader), loss.data.item()))
                        else:
                            if hasattr(Config().trainer, 'use_wandb'):
                                wandb.log({"batch loss": loss.data.item()})

                            logging.info(
                                "[Client #{}] Epoch: [{}/{}][{}/{}]\tLoss: {:.6f}"
                                .format(self.client_id, epoch, epochs,
                                        batch_id, len(train_loader),
                                        loss.data.item()))
                if hasattr(optimizer, "params_state_update"):
                    optimizer.params_state_update()

        self.model.cpu()

        model_type = Config().trainer.model_name
        filename = f"{model_type}_{self.client_id}_{config['run_id']}.pth"
        self.save_model(filename)

        if hasattr(Config().trainer, 'use_wandb'):
            run.finish()

    def train(self, trainset, sampler, cut_layer=None):
        """The main training loop in a federated learning workload.

        Arguments:
        trainset: The training dataset.
        """
        self.start_training()
        mp.set_start_method('spawn')

        config = Config().trainer._asdict()
        config['run_id'] = Config().params['run_id']

        proc = mp.Process(target=Trainer.train_process,
                          args=(
                              self,
                              config,
                              trainset,
                              sampler,
                              cut_layer,
                          ))
        proc.start()
        proc.join()

        model_name = Config().trainer.model_name
        filename = f"{model_name}_{self.client_id}_{Config().params['run_id']}.ckpt"
        self.load_model(filename)

        self.pause_training()

    def test(self, testset):
        """Testing the model using the provided test dataset.

        Arguments:
        testset: The test dataset.
        """
        self.start_training()
        mp.set_start_method('spawn')

        config = Config().trainer._asdict()
        config['run_id'] = Config().params['run_id']

        proc = mp.Process(target=Trainer.test_process,
                          args=(
                              self,
                              config,
                              testset,
                          ))
        proc.start()
        proc.join()

        model_name = Config().trainer.model_name
        filename = f"{model_name}_{self.client_id}_{Config().params['run_id']}.acc"
        accuracy = Trainer.load_accuracy(filename)

        self.pause_training()
        return accuracy

    """A custom trainer with custom training and testing loops. """

    def train_model(self, config, trainset, sampler, cut_layer=None):  # pylint: disable=unused-argument
        """A custom training loop. """
        criterion = nn.CrossEntropyLoss()
        optimizer = optim.Adam(self.model.parameters(), lr=0.02)
        train_loader = DataLoader(dataset=trainset,
                                  batch_size=config['batch_size'],
                                  sampler=sampler)

        # Training the model using Catalyst's SupervisedRunner
        runner = dl.SupervisedRunner()

        runner.train(model=self.model,
                     criterion=criterion,
                     optimizer=optimizer,
                     loaders={"train": train_loader},
                     num_epochs=1,
                     logdir="./logs",
                     verbose=True)

    def test_model(self, config, testset):  # pylint: disable=unused-argument
        """A custom testing loop. """
        test_loader = torch.utils.data.DataLoader(
            testset, batch_size=config['batch_size'], shuffle=False)

        # Using Catalyst's SupervisedRunner and AccuracyCallback to compute accuracies
        runner = dl.SupervisedRunner()
        runner.train(model=self.model,
                     num_epochs=1,
                     loaders={"valid": test_loader},
                     logdir="./logs",
                     verbose=True,
                     callbacks=[
                         dl.AccuracyCallback(input_key="logits",
                                             target_key="targets",
                                             num_classes=10)
                     ])

        # Retrieving the top-1 accuracy from SupervisedRunner
        accuracy = runner.epoch_metrics["valid"]["accuracy"]
        return accuracy