# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from __future__ import print_function, absolute_import
import argparse
import os
import os.path as osp
import numpy as np
import sys

import torch
from torch import nn
from torch.backends import cudnn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from datetime import datetime
# from torchsummary import summary

from reid import data_manager
from reid import models
from reid.img_trainers import ImgTrainer
from reid.img_evaluators import ImgEvaluator
from reid.loss.loss_set import TripletHardLoss, CrossEntropyLabelSmoothLoss
from reid.utils.data import transforms as T
from reid.utils.data.preprocessor import Preprocessor
from reid.utils.data.sampler import RandomIdentitySampler
from reid.utils.serialization import load_checkpoint, save_checkpoint
from reid.utils.lr_scheduler import LRScheduler

sys.path.append(os.path.join(os.path.dirname(__file__)))

os.environ['CUDA_VISIBLE_DEVICES'] = '0'

def get_data(name, data_dir, height, width, batch_size, num_instances,
             workers):
    # Datasets
    if name == 'VRU':
        dataset_name = 'VRU'
        dataset = data_manager.init_imgreid_dataset(
            root=data_dir, name=dataset_name
        )
        dataset.images_dir = osp.join(data_dir, 'Pic')
    # Num. of training IDs
    num_classes = dataset.num_train_cids if name == "VRU" else dataset.num_train_pids

    train_transformer = T.Compose([
        T.Random2DTranslation(height, width),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
    ])

    test_transformer = T.Compose([
        T.RectScale(height, width),
        T.ToTensor(),
    ])

    train_loader = DataLoader(
        Preprocessor(dataset.train, root=dataset.images_dir, transform=train_transformer),
        batch_size=batch_size, num_workers=workers,
        sampler=RandomIdentitySampler(dataset.train, num_instances),
        pin_memory=True, drop_last=True)

    query_loader = DataLoader(
        Preprocessor(dataset.query, root=dataset.images_dir, transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    gallery_loader = DataLoader(
        Preprocessor(dataset.gallery, root=dataset.images_dir, transform=test_transformer),
        batch_size=batch_size, num_workers=workers,
        shuffle=False, pin_memory=True)

    return dataset, num_classes, train_loader, query_loader, gallery_loader


# 写入txt
class Logger(object):
    def __init__(self, filename='default.log', stream=sys.stdout):
        self.terminal = stream
        self.log = open(filename, 'a')

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)

    def flush(self):
        pass


def main(args):
    # Set the seeds
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    cudnn.benchmark = True
    checkpoint = "checkpoint_88.pth"
    # 设置日志输出路径
    theTime = datetime.now().strftime('%Y_%m_%d_%H_%M_%S')
    if args.evaluate:
        sys.stdout = Logger(f'./logs/Test/GASNet_Test_{theTime}_{args.use_o_scale}_{args.dataset}.log', sys.stdout)
    else:
        sys.stdout = Logger(f'./logs/Train/GASNet_Train_{theTime}_{args.use_o_scale}_{args.dataset}.log', sys.stdout)

    # Create data loaders
    assert args.num_instances > 1, "num_instances should be greater than 1"
    assert args.batch_size % args.num_instances == 0, \
        'num_instances should divide batch_size'
    if args.height is None or args.width is None:
        args.height, args.width = (144, 56) if args.arch == 'inception' else \
            (256, 128)

    dataset, num_classes, train_loader, query_loader, gallery_loader = \
        get_data(args.dataset, args.data_dir, args.height,
                 args.width, args.batch_size, args.num_instances, args.workers)

    # Summary Writer
    if not args.evaluate:
        TIMESTAMP = "{0:%Y-%m-%dT%H-%M-%S/}".format(datetime.now())
        log_dir = osp.join(args.logs_dir, 'tensorboard_log' + TIMESTAMP)
        print(log_dir)
        summary_writer = SummaryWriter(log_dir)
    else:
        summary_writer = None

    # Create model
    model = models.create(args.arch, pretrained=False, num_feat=args.features,
                          height=args.height, width=args.width, dropout=args.dropout,
                          num_classes=num_classes, branch_name=args.branch_name, use_o_scale=args.use_o_scale)

    # Load from checkpoint
    start_epoch = best_top1 = 0
    device_ids = [0]

    # test/evaluate the model
    if args.evaluate:
        if args.use_o_scale:
            evaluate_weight = torch.load(args.checkpoint)
        else:
            evaluate_weight = torch.load(args.checkpoint)
        # model.eval()
        model.load_state_dict(evaluate_weight)
        model = nn.DataParallel(model, device_ids=device_ids).cuda()
        evaluator = ImgEvaluator(model, file_path=args.logs_dir, use_o_scale=args.use_o_scale)
        if args.use_o_scale:
            feats_list = ['feat_gasnet', 'feat_gasnet_', 'feat_cls']
            evaluator.eval_worerank(query_loader, gallery_loader, dataset.query, dataset.gallery,
                                    metric=['cosine', 'euclidean'],
                                    types_list=feats_list)
            return
        else:
            feats_list = ['feat_rga', 'feat_rga_']
            evaluator.eval_worerank(query_loader, gallery_loader, dataset.query, dataset.gallery,
                                    metric=['cosine', 'euclidean'],
                                    types_list=feats_list)
            return
    elif args.resume:
        torch.cuda.set_device(0)
        checkpoint = load_checkpoint(f'./logs/RGA-SC/{args.dataset}/checkpoint_88_88.pth.tar')
        model.load_state_dict(checkpoint['state_dict'])
        model = nn.DataParallel(model, device_ids=device_ids).cuda()
        start_epoch = checkpoint['epoch']
        best_top1 = checkpoint['best_top1']
        print("=> Start epoch {}  best top1 {:.1%}".format(start_epoch, best_top1))
        print("=> Start epoch {}".format(start_epoch))

    else:
        print("=> Start train a new model!!")
        pre_train_weight = torch.load(f'./weights/pre_train/resnet50-19c8e357.pth')
        model.load_state_dict(pre_train_weight, strict=False)
        model = nn.DataParallel(model, device_ids=device_ids).cuda()

        # model = nn.DataParallel(model, device_ids=device_ids).cuda()
    # Criterion
    criterion_cls = CrossEntropyLabelSmoothLoss(num_classes).cuda()
    criterion_tri = TripletHardLoss(margin=args.margin)
    criterion = [criterion_cls, criterion_tri]

    # Trainer
    trainer = ImgTrainer(model, criterion, summary_writer, use_o_scale=args.use_o_scale)

    # Optimizer
    if hasattr(model.module, 'backbone'):
        base_param_ids = set(map(id, model.module.backbone.parameters()))
        new_params = [p for p in model.parameters() if id(p) not in base_param_ids]
        param_groups = [
            {'params': filter(lambda p: p.requires_grad, model.module.backbone.parameters()), 'lr_mult': 1.0},
            {'params': filter(lambda p: p.requires_grad, new_params), 'lr_mult': 1.0}]
    else:
        param_groups = model.parameters()
    if args.optimizer == 'sgd':
        optimizer = torch.optim.SGD(param_groups, lr=args.lr,
                                    momentum=args.momentum,
                                    weight_decay=args.weight_decay,
                                    nesterov=True)
    elif args.optimizer == 'adam':
        optimizer = torch.optim.Adam(
            param_groups, lr=args.lr, weight_decay=args.weight_decay
        )
    else:
        raise NameError
    # if args.resume and checkpoint.has_key('optimizer'):
    #     optimizer.load_state_dict(checkpoint['optimizer'])
    if args.resume and 'optimizer' in checkpoint:
        optimizer.load_state_dict(checkpoint['optimizer'])

    # Learning rate scheduler
    lr_scheduler = LRScheduler(base_lr=0.0008, step=[88, 95, 105, 115, 125, 130, 135, 140, 145, 150, 155, 160, 165, 170, 175, 180],
                               factor=0.5, warmup_epoch=15,
                               warmup_begin_lr=0.000008)

    # Start training
    for epoch in range(start_epoch, args.epochs):
        lr = lr_scheduler.update(epoch)
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
        print('[Info] Epoch [{}] learning rate update to {:.3e}'.format(epoch+1, lr))
        trainer.train(epoch, train_loader, optimizer, random_erasing=args.random_erasing, empty_cache=args.empty_cache)
        if (epoch + 1) % 1 == 0 and (epoch + 1) >= args.start_save:
            is_best = False
            save_checkpoint({
                'state_dict': model.module.state_dict(),
                'epoch': epoch + 1,
                'best_top1': best_top1,
                'optimizer': optimizer.state_dict(),
            }, epoch + 1, is_best, save_interval=1, fpath=osp.join(args.logs_dir, f'{args.dataset}/checkpoint_{epoch + 1}.pth.tar'))
            if args.use_o_scale:
                torch.save(model.module.state_dict(), f'./weights/{args.dataset}/use_o_scale/checkpoint_{epoch + 1}.pth')
            else:
                torch.save(model.module.state_dict(), f'./weights/{args.dataset}/use_rga/checkpoint_{epoch + 1}.pth')

        print("-----------Start validate!!!-------------")
        if args.use_o_scale:
            evaluate_weight = torch.load(f'./weights/{args.dataset}/use_o_scale/checkpoint_{epoch + 1}.pth')
        else:
            evaluate_weight = torch.load(f'./weights/{args.dataset}/use_rga/checkpoint_{epoch + 1}.pth')
        # model.eval()
        model.load_state_dict(evaluate_weight, strict=False)
        evaluator = ImgEvaluator(model, file_path=args.logs_dir, use_o_scale=args.use_o_scale)
        if args.use_o_scale:
            feats_list = ['feat_gasnet', 'feat_gasnet_', 'feat_cls']
            evaluator.eval_worerank(query_loader, gallery_loader, dataset.query, dataset.gallery,
                                    metric=['cosine', 'euclidean'],
                                    types_list=feats_list)
        else:
            feats_list = ['feat_rga', 'feat_rga_']
            evaluator.eval_worerank(query_loader, gallery_loader, dataset.query, dataset.gallery,
                                    metric=['cosine', 'euclidean'],
                                    types_list=feats_list)

if __name__ == '__main__':
    torch.cuda.empty_cache()
    # os.environ['CUDA_VISIBLE_DEVICES'] = '0,1'
    def str2bool(v):
        if v.lower() in ('yes', 'true', 't', 'y', '1'):
            return True
        elif v.lower() in ('no', 'false', 'f', 'n', '0'):
            return False
        else:
            raise argparse.ArgumentTypeError('Unsupported value encountered.')


    parser = argparse.ArgumentParser(description="Softmax loss classification")
    # data
    parser.add_argument('-d', '--dataset', type=str, default='VRU')
    parser.add_argument('-b', '--batch-size', type=int, default=24)
    parser.add_argument('-j', '--workers', type=int, default=0)
    parser.add_argument('--height', type=int,
                        help="input height, default: 256 for resnet*, "
                             "144 for inception")
    parser.add_argument('--width', type=int,
                        help="input width, default: 128 for resnet*, "
                             "56 for inception")
    parser.add_argument('--combine-trainval', action='store_true',
                        help="train and val sets together for training, "
                             "val set alone for validation")
    parser.add_argument('--num-instances', type=int, default=4,
                        help="each minibatch consist of "
                             "(batch_size // num_instances) identities, and "
                             "each identity has num_instances instances, "
                             "default: 4")
    # model
    parser.add_argument('-a', '--arch', type=str, default='resnet50_rga',
                        choices=models.names())
    parser.add_argument('--features', type=int, default=2048)
    parser.add_argument('--dropout', type=float, default=0)
    parser.add_argument('--branch_name', type=str, default='rgasc')
    parser.add_argument('--use_rgb', type=str2bool, default=True)
    parser.add_argument('--use_bn', type=str2bool, default=True)
    parser.add_argument('--use_o_scale', type=str2bool, default=True)
    # loss
    parser.add_argument('--margin', type=float, default=0.3,
                        help="margin of the triplet loss, default: 0.3")
    # optimizer
    parser.add_argument('-opt', '--optimizer', type=str, default='adam')
    parser.add_argument('--lr', type=float, default=0.1,
                        help="learning rate of new parameters, for pretrained "
                             "parameters it is 10 times smaller than this")
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight-decay', type=float, default=5e-4)
    # training configs
    parser.add_argument('--num_gpu', type=int, default=1)
    parser.add_argument('--resume', action='store_true',
                        help='continue to train')
    parser.add_argument('--rerank', action='store_true',
                        help="evaluation with re-ranking")
    parser.add_argument('--epochs', type=int, default=180)
    parser.add_argument('--start_save', type=int, default=1,
                        help="start saving checkpoints after specific epoch")
    parser.add_argument('--seed', type=int, default=16)
    parser.add_argument('--print-freq', type=int, default=1)
    parser.add_argument('--empty_cache', type=str2bool, default=False)
    parser.add_argument('--random_erasing', type=str2bool, default=True)
    # testing configs
    parser.add_argument('--evaluate', action='store_true',
                        help="evaluation only")
    parser.add_argument('--checkpoint', type=str, default="",
                        help="load the checkpoint for testing")
    # metric learning
    parser.add_argument('--dist-metric', type=str, default='euclidean',
                        choices=['euclidean', 'kissme'])
    # misc
    working_dir = osp.dirname(osp.abspath(__file__))
    parser.add_argument('--data-dir', type=str, metavar='PATH',
                        default='./dataset/')
    parser.add_argument('--logs-dir', type=str, metavar='PATH',
                        default="./logs/RGA-SC/")
    parser.add_argument('--logs-file', type=str, metavar='PATH',
                        default='log.txt')
    main(parser.parse_args())