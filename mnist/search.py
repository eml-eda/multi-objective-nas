#*----------------------------------------------------------------------------*
#* Copyright (C) 2022 Politecnico di Torino, Italy                            *
#* SPDX-License-Identifier: Apache-2.0                                        *
#*                                                                            *
#* Licensed under the Apache License, Version 2.0 (the "License");            *
#* you may not use this file except in compliance with the License.           *
#* You may obtain a copy of the License at                                    *
#*                                                                            *
#* http://www.apache.org/licenses/LICENSE-2.0                                 *
#*                                                                            *
#* Unless required by applicable law or agreed to in writing, software        *
#* distributed under the License is distributed on an "AS IS" BASIS,          *
#* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.   *
#* See the License for the specific language governing permissions and        *
#* limitations under the License.                                             *
#*                                                                            *
#* Author:  Matteo Risso <matteo.risso@polito.it>                             *
#*----------------------------------------------------------------------------*
import argparse
import copy
import sys
sys.path.append('..')

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import datasets, transforms
from torch.optim.lr_scheduler import StepLR

import models as models

# Simply parse all models' names contained in models directory
model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

def main():
    # Training settings
    parser = argparse.ArgumentParser(description='PyTorch MNIST Search')
    parser.add_argument('-a', '--arch', metavar='ARCH', default='plain_cnn',
                        choices=model_names,
                        help='model architecture: ' +
                            ' | '.join(model_names) +
                            ' (default: plain_cnn)')
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=14, metavar='N',
                        help='number of epochs to train (default: 14)')
    parser.add_argument('--lr', type=float, default=1.0, metavar='LR',
                        help='learning rate (default: 1.0)')
    parser.add_argument('--cd-size', type=float, default=0.0, metavar='CD',
                        help='complexity decay size (default: 0.0)')
    parser.add_argument('--cd-ops', type=float, default=0.0, metavar='CD',
                        help='complexity decay ops (default: 0.0)')
    parser.add_argument('--size-target', type=float, default=0, metavar='ST',
                        help='target size (default: 0)')
    parser.add_argument('--gamma', type=float, default=0.7, metavar='M',
                        help='Learning rate step gamma (default: 0.7)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--eval-complexity', action='store_true', default=False,
                        help='Evaluate complexity of initial model and exit')
    parser.add_argument('--dry-run', action='store_true', default=False,
                        help='quickly check a single pass')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()

    torch.manual_seed(args.seed)

    device = torch.device("cuda" if use_cuda else "cpu")

    train_kwargs = {'batch_size': args.batch_size}
    test_kwargs = {'batch_size': args.test_batch_size}
    if use_cuda:
        cuda_kwargs = {'num_workers': 1,
                       'pin_memory': True,
                       'shuffle': True}
        train_kwargs.update(cuda_kwargs)
        test_kwargs.update(cuda_kwargs)

    transform=transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.1307,), (0.3081,))
        ])
    dataset1 = datasets.MNIST('data', train=True, download=True,
                       transform=transform)
    dataset2 = datasets.MNIST('data', train=False,
                       transform=transform)
    train_loader = torch.utils.data.DataLoader(dataset1,**train_kwargs)
    test_loader = torch.utils.data.DataLoader(dataset2, **test_kwargs)

    print("=> creating model '{}'".format(args.arch))
    model = models.__dict__[args.arch]().to(device)
    optimizer = optim.Adadelta(model.parameters(), lr=args.lr)

    scheduler = StepLR(optimizer, step_size=1, gamma=args.gamma)

    # Dummy test step to get inital complexities of model
    test(model, device, test_loader)
    size_i = sum(model.size_dict.values()).clone().detach().cpu().numpy()
    ops_i = sum(model.ops_dict.values()).clone().detach().cpu().numpy()
    print(f"Initial size: {size_i:.3e} params\tInitial ops: {ops_i:.3e} OPs")
    alive_ch_i = copy.deepcopy(model.alive_ch)
    for k, v in alive_ch_i.items():
        print(f"{k}:\t{int(v)+1} channels")
    if args.eval_complexity:
        print("Exit...")
        return

    for epoch in range(1, args.epochs + 1):
        train(args, model, device, train_loader, optimizer, epoch)
        test(model, device, test_loader)
        scheduler.step()
    
    # Compute and print final size and ops
    size_f = sum(model.size_dict.values()).clone().detach().cpu().numpy()
    ops_f = sum(model.ops_dict.values()).clone().detach().cpu().numpy()
    print(f"Final size: {size_f:.3e}/{size_i:.3e} parameters\tFinal ops: {ops_f:.3e}/{ops_i:.3e} OPs")
    # Print learned alive channels
    for k, v in model.alive_ch.items():
        print(f"{k}:\t{int(v)+1}/{int(alive_ch_i[k])+1} channels")

    # Save model
    torch.save(model.state_dict(), 
        f"saved_models/srch_{args.arch}_target-{args.size_target:.1e}_cdops-{args.cd_ops:.1e}.pth.tar")

def train(args, model, device, train_loader, optimizer, epoch):
    model.train()
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = F.nll_loss(output, target)
        # Compute size-complexity loss with constraint
        loss_size = torch.abs(sum(model.size_dict.values()) - args.size_target)
        loss_size *= args.cd_size
        # Compute ops-complexity loss with constraint
        loss_ops = sum(model.ops_dict.values())
        loss_ops *= args.cd_ops
        # Sum different losses
        loss += loss_size + loss_ops
        loss.backward()
        optimizer.step()
        if batch_idx % args.log_interval == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}\tSize-Loss: {:.6f}\tOps-Loss: {:.6f}'.format(
                epoch, batch_idx * len(data), len(train_loader.dataset),
                100. * batch_idx / len(train_loader), loss.item(), loss_size.item(), loss_ops.item()))
            if args.dry_run:
                break

def test(model, device, test_loader):
    model.eval()
    test_loss = 0
    correct = 0
    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)
            output = model(data)
            test_loss += F.nll_loss(output, target, reduction='sum').item()  # sum up batch loss
            pred = output.argmax(dim=1, keepdim=True)  # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()

    test_loss /= len(test_loader.dataset)

    print('\nTest set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n'.format(
        test_loss, correct, len(test_loader.dataset),
        100. * correct / len(test_loader.dataset)))

if __name__ == '__main__':
    main()
