# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
import argparse
import math
import os
import shutil
import time
from logging import getLogger

import numpy as np
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim
import apex
from apex.parallel.LARC import LARC

from swav.utils import (
    bool_flag,
    initialize_exp,
    restart_from_checkpoint,
    fix_random_seeds,
    AverageMeter,
    init_distributed_mode,
)

from swav.multicropdataset import MultiCropDataset
from swav.swav_transforms import SwAVTrainDataTransform
from swav.stl10_datamodule import STL10DataModule, stl10_normalization
import swav.resnet50 as resnet_models

from torch.utils.tensorboard import SummaryWriter

# single node single GPU training
os.environ["RANK"] = '0'
os.environ["WORLD_SIZE"] = '1'
os.environ["MASTER_ADDR"] = '127.0.0.1'
os.environ['MASTER_PORT'] = '29500'

logger = getLogger()

parser = argparse.ArgumentParser(description="Implementation of SwAV")

#########################
#### data parameters ####
#########################
parser.add_argument("--gaussian_blur", type=bool, default=True,
                    help="select gaussian blur in augmentation")
parser.add_argument("--jitter_strength", type=float, default=1.,
                    help="jitter strength")
parser.add_argument("--dataset", type=str, default='stl10',
                    help="choose between imagenet, stl10")
parser.add_argument("--data_path", type=str, default=".",
                    help="path to dataset repository")
parser.add_argument("--nmb_crops", type=int, default=[2, 4], nargs="+",
                    help="list of number of crops (example: [2, 6])")
parser.add_argument("--size_crops", type=int, default=[96, 36], nargs="+",
                    help="crops resolutions (example: [224, 96])")
parser.add_argument("--min_scale_crops", type=float, default=[0.33, 0.10], nargs="+",
                    help="argument in RandomResizedCrop (example: [0.14, 0.05])")
parser.add_argument("--max_scale_crops", type=float, default=[1, 0.33], nargs="+",
                    help="argument in RandomResizedCrop (example: [1., 0.14])")

#########################
## swav specific params #
#########################
parser.add_argument("--crops_for_assign", type=int, nargs="+", default=[0, 1],
                    help="list of crops id used for computing assignments")
parser.add_argument("--temperature", default=0.1, type=float,
                    help="temperature parameter in training loss")
parser.add_argument("--epsilon", default=0.05, type=float,
                    help="regularization parameter for Sinkhorn-Knopp algorithm")
parser.add_argument("--sinkhorn_iterations", default=3, type=int,
                    help="number of iterations in Sinkhorn-Knopp algorithm")
parser.add_argument("--feat_dim", default=128, type=int,
                    help="feature dimension")
parser.add_argument("--nmb_prototypes", default=256, type=int,
                    help="number of prototypes")
parser.add_argument("--queue_length", type=int, default=3840,
                    help="length of the queue (0 for no queue)")
parser.add_argument("--epoch_queue_starts", type=int, default=15,
                    help="from this epoch, we start using a queue")

#########################
#### optim parameters ###
#########################
parser.add_argument("--optimizer", default="adam", type=str,
                    help="choose between adam/sgd")
parser.add_argument('--exclude_bn_bias', default=False, type=bool,
                    help="exclude bn/bias layers from weight decay")
parser.add_argument("--epochs", default=100, type=int,
                    help="number of total epochs to run")
parser.add_argument("--batch_size", default=128, type=int,
                    help="batch size per gpu, i.e. how many unique instances per gpu")
parser.add_argument("--base_lr", default=4.8, type=float, help="base learning rate")
parser.add_argument("--final_lr", type=float, default=1e-6, help="final learning rate")
parser.add_argument("--freeze_prototypes_niters", default=206, type=int,
                    help="freeze the prototypes during this many iterations from the start")
parser.add_argument("--wd", default=1e-6, type=float, help="weight decay")
parser.add_argument("--warmup_epochs", default=10, type=int, help="number of warmup epochs")
parser.add_argument("--start_warmup", default=0, type=float,
                    help="initial warmup learning rate")

#########################
#### dist parameters ###
#########################
parser.add_argument("--dist_url", default="env://", type=str, help="""url used to set up distributed
                    training; see https://pytorch.org/docs/stable/distributed.html""")
parser.add_argument("--world_size", default=-1, type=int, help="""
                    number of processes: it is set automatically and
                    should not be passed as argument""")
parser.add_argument("--rank", default=0, type=int, help="""rank of this process:
                    it is set automatically and should not be passed as argument""")
parser.add_argument("--local_rank", default=0, type=int,
                    help="this argument is not used and should be ignored")

#########################
#### other parameters ###
#########################
parser.add_argument("--arch", default="resnet50", type=str, help="convnet architecture")
parser.add_argument("--hidden_mlp", default=2048, type=int,
                    help="hidden layer dimension in projection head")
parser.add_argument("--workers", default=16, type=int,
                    help="number of data loading workers")
parser.add_argument("--checkpoint_freq", type=int, default=20,
                    help="Save the model periodically")
parser.add_argument("--use_fp16", type=bool_flag, default=True,
                    help="whether to train with mixed precision or not")
parser.add_argument("--sync_bn", type=str, default="pytorch", help="synchronize bn")
parser.add_argument("--dump_path", type=str, default=".",
                    help="experiment dump path for checkpoints and log")
parser.add_argument("--seed", type=int, default=31, help="seed")


def exclude_from_wt_decay(named_params, weight_decay, skip_list=['bias', 'bn']):
    params = []
    excluded_params = []

    for name, param in named_params:
        if not param.requires_grad:
            continue
        elif any(layer_name in name for layer_name in skip_list):
            excluded_params.append(param)
        else:
            params.append(param)

    return [
        {'params': params, 'weight_decay': weight_decay},
        {'params': excluded_params, 'weight_decay': 0.}
    ]


def main():
    global args
    args = parser.parse_args()
    init_distributed_mode(args)
    fix_random_seeds(args.seed)
    logger, training_stats = initialize_exp(args, "epoch", "loss")
    writer = SummaryWriter()

    # build data
    if args.dataset == 'imagenet':
        train_dataset = MultiCropDataset(
            args.data_path,
            args.size_crops,
            args.nmb_crops,
            args.min_scale_crops,
            args.max_scale_crops,
        )
        sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
        train_loader = torch.utils.data.DataLoader(
            train_dataset,
            sampler=sampler,
            batch_size=args.batch_size,
            num_workers=args.workers,
            pin_memory=True,
            drop_last=True
        )
    elif args.dataset == 'stl10':
        swav_train_transform = SwAVTrainDataTransform(
            normalize=stl10_normalization(),
            size_crops=args.size_crops,
            nmb_crops=args.nmb_crops,
            min_scale_crops=args.min_scale_crops,
            max_scale_crops=args.max_scale_crops,
            gaussian_blur=args.gaussian_blur,
            jitter_strength=args.jitter_strength
        )

        datamodule = STL10DataModule(
            data_dir=args.data_path,
            train_dist_sampler=True,
            num_workers=args.workers,
            batch_size=args.batch_size
        )

        datamodule.prepare_data()
        datamodule.setup()

        datamodule.train_dataloader = datamodule.train_dataloader_mixed
        datamodule.train_transforms = swav_train_transform
        train_loader = datamodule.train_dataloader_mixed()

    if args.dataset == 'imagenet':
        logger.info("Building data done with {} images loaded.".format(len(train_dataset)))
    elif args.dataset == 'stl10':
        logger.info("Building data done with {} images loaded.".format(
            datamodule.num_unlabeled_samples + datamodule.num_labeled_samples
        ))

    # build model
    model = resnet_models.__dict__[args.arch](
        normalize=True,
        hidden_mlp=args.hidden_mlp,
        output_dim=args.feat_dim,
        nmb_prototypes=args.nmb_prototypes,
    )

    if args.dataset == 'stl10':
        model.maxpool = nn.MaxPool2d(kernel_size=1, stride=1)

    # synchronize batch norm layers
    if args.sync_bn == "pytorch":
        model = nn.SyncBatchNorm.convert_sync_batchnorm(model)
    elif args.sync_bn == "apex":
        process_group = None
        if args.world_size // 8 > 0:
            process_group = apex.parallel.create_syncbn_process_group(args.world_size // 8)
        model = apex.parallel.convert_syncbn_model(model, process_group=process_group)
    # copy model to GPU
    model = model.cuda()
    if args.rank == 0:
        logger.info(model)
    logger.info("Building model done.")

    params = None
    if args.exclude_bn_bias:
        params = exclude_from_wt_decay(
            model.named_parameters(),
            weight_decay=args.wd
        )

    # build optimizer
    if args.optimizer == 'sgd':
        optimizer = torch.optim.SGD(
            params if args.exclude_bn_bias else model.parameters(),
            lr=args.base_lr,
            momentum=0.9,
            weight_decay=args.wd,
        )
    elif args.optimizer == 'adam':
        optimizer = torch.optim.Adam(
            params if args.exclude_bn_bias else model.parameters(),
            lr=args.base_lr,
            weight_decay=args.wd
        )

    optimizer = LARC(optimizer=optimizer, trust_coefficient=0.001, clip=False)
    warmup_lr_schedule = np.linspace(args.start_warmup, args.base_lr, len(train_loader) * args.warmup_epochs)
    iters = np.arange(len(train_loader) * (args.epochs - args.warmup_epochs))
    cosine_lr_schedule = np.array([args.final_lr + 0.5 * (args.base_lr - args.final_lr) * (1 + \
                         math.cos(math.pi * t / (len(train_loader) * (args.epochs - args.warmup_epochs)))) for t in iters])
    lr_schedule = np.concatenate((warmup_lr_schedule, cosine_lr_schedule))
    logger.info("Building optimizer done.")

    # init mixed precision
    if args.use_fp16:
        model, optimizer = apex.amp.initialize(model, optimizer, opt_level="O1")
        logger.info("Initializing mixed precision done.")

    # wrap model
    model = nn.parallel.DistributedDataParallel(
        model,
        device_ids=[args.gpu_to_work_on],
        find_unused_parameters=True,
    )

    # optionally resume from a checkpoint
    to_restore = {"epoch": 0}
    restart_from_checkpoint(
        os.path.join(args.dump_path, "checkpoint.pth.tar"),
        run_variables=to_restore,
        state_dict=model,
        optimizer=optimizer,
        amp=apex.amp,
    )
    start_epoch = to_restore["epoch"]

    # build the queue
    queue = None
    queue_path = os.path.join(args.dump_path, "queue" + str(args.rank) + ".pth")
    if os.path.isfile(queue_path):
        queue = torch.load(queue_path)["queue"]
    # the queue needs to be divisible by the batch size
    args.queue_length -= args.queue_length % (args.batch_size * args.world_size)

    cudnn.benchmark = True

    for epoch in range(start_epoch, args.epochs):

        # train the network for one epoch
        logger.info("============ Starting epoch %i ... ============" % epoch)

        # set sampler
        train_loader.sampler.set_epoch(epoch)

        # optionally starts a queue
        if args.queue_length > 0 and epoch >= args.epoch_queue_starts and queue is None:
            queue = torch.zeros(
                len(args.crops_for_assign),
                args.queue_length // args.world_size,
                args.feat_dim,
            ).cuda()

        # train the network
        scores, queue = train(train_loader, model, optimizer, epoch, lr_schedule, queue)
        training_stats.update(scores)
        writer.add_scalar("Loss/train", scores[1], scores[0])

        # save checkpoints
        if args.rank == 0:
            save_dict = {
                "epoch": epoch + 1,
                "state_dict": model.state_dict(),
                "optimizer": optimizer.state_dict(),
            }
            if args.use_fp16:
                save_dict["amp"] = apex.amp.state_dict()
            torch.save(
                save_dict,
                os.path.join(args.dump_path, "checkpoint.pth.tar"),
            )
            if epoch % args.checkpoint_freq == 0 or epoch == args.epochs - 1:
                shutil.copyfile(
                    os.path.join(args.dump_path, "checkpoint.pth.tar"),
                    os.path.join(args.dump_checkpoints, "ckp-" + str(epoch) + ".pth"),
                )
        if queue is not None:
            torch.save({"queue": queue}, queue_path)

    writer.flush()


def train(train_loader, model, optimizer, epoch, lr_schedule, queue):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()

    softmax = nn.Softmax(dim=1).cuda()
    model.train()
    use_the_queue = False

    end = time.time()
    for it, inputs in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        # update learning rate
        iteration = epoch * len(train_loader) + it
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr_schedule[iteration]

        # normalize the prototypes
        with torch.no_grad():
            w = model.module.prototypes.weight.data.clone()
            w = nn.functional.normalize(w, dim=1, p=2)
            model.module.prototypes.weight.copy_(w)

        # ============ multi-res forward passes ... ============
        embedding, output = model(inputs)
        embedding = embedding.detach()
        bs = inputs[0].size(0)

        # ============ swav loss ... ============
        loss = 0
        for i, crop_id in enumerate(args.crops_for_assign):
            with torch.no_grad():
                out = output[bs * crop_id: bs * (crop_id + 1)]

                # time to use the queue
                if queue is not None:
                    if use_the_queue or not torch.all(queue[i, -1, :] == 0):
                        use_the_queue = True
                        out = torch.cat((torch.mm(
                            queue[i],
                            model.module.prototypes.weight.t()
                        ), out))
                    # fill the queue
                    queue[i, bs:] = queue[i, :-bs].clone()
                    queue[i, :bs] = embedding[crop_id * bs: (crop_id + 1) * bs]
                # get assignments
                q = torch.exp(out / args.epsilon).t()
                q = distributed_sinkhorn(q, args.sinkhorn_iterations)[-bs:]

            # cluster assignment prediction
            subloss = 0
            for v in np.delete(np.arange(np.sum(args.nmb_crops)), crop_id):
                p = softmax(output[bs * v: bs * (v + 1)] / args.temperature)
                subloss -= torch.mean(torch.sum(q * torch.log(p), dim=1))
            loss += subloss / (np.sum(args.nmb_crops) - 1)
        loss /= len(args.crops_for_assign)

        # ============ backward and optim step ... ============
        optimizer.zero_grad()
        if args.use_fp16:
            with apex.amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        # cancel some gradients
        if iteration < args.freeze_prototypes_niters:
            for name, p in model.named_parameters():
                if "prototypes" in name:
                    p.grad = None
        optimizer.step()

        # ============ misc ... ============
        losses.update(loss.item(), inputs[0].size(0))
        batch_time.update(time.time() - end)
        end = time.time()
        if args.rank ==0 and it % 50 == 0:
            logger.info(
                "Epoch: [{0}][{1}]\t"
                "Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t"
                "Data {data_time.val:.3f} ({data_time.avg:.3f})\t"
                "Loss {loss.val:.4f} ({loss.avg:.4f})\t"
                "Lr: {lr:.4f}".format(
                    epoch,
                    it,
                    batch_time=batch_time,
                    data_time=data_time,
                    loss=losses,
                    lr=optimizer.optim.param_groups[0]["lr"],
                )
            )
    return (epoch, losses.avg), queue


def distributed_sinkhorn(Q, nmb_iters):
    with torch.no_grad():
        sum_Q = torch.sum(Q)
        dist.all_reduce(sum_Q)
        Q /= sum_Q

        u = torch.zeros(Q.shape[0]).cuda(non_blocking=True)
        r = torch.ones(Q.shape[0]).cuda(non_blocking=True) / Q.shape[0]
        c = torch.ones(Q.shape[1]).cuda(non_blocking=True) / (args.world_size * Q.shape[1])

        curr_sum = torch.sum(Q, dim=1)
        dist.all_reduce(curr_sum)

        for it in range(nmb_iters):
            u = curr_sum
            Q *= (r / u).unsqueeze(1)
            Q *= (c / torch.sum(Q, dim=0)).unsqueeze(0)
            curr_sum = torch.sum(Q, dim=1)
            dist.all_reduce(curr_sum)
        return (Q / torch.sum(Q, dim=0, keepdim=True)).t().float()


if __name__ == "__main__":
    main()
