# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import math
from typing import Any, Dict, List, Tuple, Union

import numpy as np
import torch
from torch import Tensor
import torch.nn.functional as F

from nni.algorithms.compression.v2.pytorch.base import Pruner
from nni.compression.pytorch.utils.shape_dependency import ChannelDependency, GroupDependency

from .base import SparsityAllocator


class NormalSparsityAllocator(SparsityAllocator):
    """
    This allocator simply pruned the weight with smaller metrics in layer level.
    """
    def generate_sparsity(self, metrics: Dict[str, Tensor]) -> Dict[str, Dict[str, Tensor]]:
        masks = {}
        for name, wrapper in self.pruner.get_modules_wrapper().items():
            sparsity_rate = wrapper.config['total_sparsity']

            assert name in metrics, 'Metric of {} is not calculated.'.format(name)

            # We assume the metric value are all positive right now.
            metric = metrics[name]
            if self.continuous_mask:
                metric *= self._compress_mask(wrapper.weight_mask)
            prune_num = int(sparsity_rate * metric.numel())
            if prune_num == 0:
                threshold = metric.min() - 1
            else:
                threshold = torch.topk(metric.view(-1), prune_num, largest=False)[0].max()
            mask = torch.gt(metric, threshold).type_as(metric)
            masks[name] = self._expand_mask(name, mask)
            if self.continuous_mask:
                masks[name]['weight'] *= wrapper.weight_mask
        return masks


class BlockSparsityAllocator(SparsityAllocator):
    """
    This allocator simply pruned the block weight with smaller metrics in layer level.
    """
    def generate_sparsity(self, metrics: Dict[str, Tensor]) -> Dict[str, Dict[str, Tensor]]:
        masks = {}
        for name, wrapper in self.pruner.get_modules_wrapper().items():
            # wrapper拿到的其实就是config_list
            sparsity_rate = wrapper.config['total_sparsity']    # total sparsity 应该是由scheduler根据 config 里的 sparsity 慢慢往上涨

            assert name in metrics, 'Metric of {} is not calculated.'.format(name)

            # We assume the metric value are all positive right now.
            metric = metrics[name]
            if self.continuous_mask:
                metric *= self._compress_mask(wrapper.weight_mask)
            prune_num = int(sparsity_rate * metric.numel())
            if prune_num == 0:
                threshold = metric.min() - 1
            else:
                threshold = torch.topk(metric.view(-1), prune_num, largest=False)[0].max()
            mask = torch.gt(metric, threshold).type_as(metric)
            masks[name] = self._expand_mask(name, mask)
            if self.continuous_mask:
                masks[name]['weight'] *= wrapper.weight_mask
        return masks

    def _compress_mask(self, mask: Tensor) -> Tensor:
        """
        This function will reduce the mask with `self.dim` and `self.block_sparse_size`.
        e.g., a mask tensor with size [50, 60, 70], self.dim is (0, 1), self.block_sparse_size is [10, 10].
        Then, the reduced mask size is [50 / 10, 60 / 10] => [5, 6].

        Parameters
        ----------
        name
            The masked module name.
        mask
            The entire mask has the same size with weight.

        Returns
        -------
        Tensor
            Reduced mask.
        """
        if self.dim is None or len(mask.size()) == 1 or len(self.dim) == len(mask.size()):
            mask = mask.clone()
        else:
            mask_dim = list(range(len(mask.size())))
            for dim in self.dim:
                mask_dim.remove(dim)
            mask = torch.sum(mask, dim=mask_dim)

        if self.block_sparse_size is not None:
            # operation like pooling
            # lower_case_letters = 'abcdefghijklmnopqrstuvwxyz'
            # ein_expression = ''
            # for i, step in enumerate(self.block_sparse_size):
            #     mask = mask.unfold(i, step, step)
            #     ein_expression += lower_case_letters[i]
            # ein_expression = '...{},{}'.format(ein_expression, ein_expression)
            # mask = torch.einsum(ein_expression, mask, torch.ones(self.block_sparse_size).to(mask.device))

            # reamain smaller block
            mask = F.avg_pool2d(mask.unsqueeze(0), kernel_size=self.block_sparse_size,
                                        ceil_mode=True, count_include_pad=False).squeeze(0)

        return (mask != 0).type_as(mask)

    def _expand_mask(self, name: str, mask: Tensor) -> Dict[str, Tensor]:
        """
        Parameters
        ----------
        name
            The masked module name.
        mask
            The reduced mask with `self.dim` and `self.block_sparse_size`.

        Returns
        -------
        Dict[str, Tensor]
            The key is `weight` or `bias`, value is the final mask.
        """
        weight_mask = mask.clone()

        if self.block_sparse_size is not None:
            # expend mask with block_sparse_size
            expand_size = list(weight_mask.size())
            reshape_size = list(weight_mask.size())
            for i, block_width in reversed(list(enumerate(self.block_sparse_size))):
                weight_mask = weight_mask.unsqueeze(i + 1)
                expand_size.insert(i + 1, block_width)
                reshape_size[i] *= block_width
            weight_mask = weight_mask.expand(expand_size).reshape(reshape_size)

        wrapper = self.pruner.get_modules_wrapper()[name]
        weight_size = wrapper.module.weight.data.size()

        # resize as weight, it will drop out tedious mask
        weight_mask = weight_mask.resize_(weight_size)

        if self.dim is None:
            assert weight_mask.size() == weight_size
            expand_mask = {'weight': weight_mask}
        else:
            # expand mask to weight size with dim
            assert len(weight_mask.size()) == len(self.dim)
            assert all(weight_size[j] == weight_mask.size(i) for i, j in enumerate(self.dim))

            idxs = list(range(len(weight_size)))
            [idxs.pop(i) for i in reversed(self.dim)]
            for i in idxs:
                weight_mask = weight_mask.unsqueeze(i)
            expand_mask = {'weight': weight_mask.expand(weight_size).clone()}
            # NOTE: assume we only mask output, so the mask and bias have a one-to-one correspondence.
            # If we support more kind of masks, this place need refactor.
            if wrapper.bias_mask is not None and weight_mask.size() == wrapper.bias_mask.size():
                expand_mask['bias'] = weight_mask.clone()

        return expand_mask


class GlobalSparsityAllocator(SparsityAllocator):
    """
    This allocator pruned the weight with smaller metrics in group level.
    This means all layers in a group will sort metrics uniformly.
    The layers with the same config in config_list is a group.

    group contains all layers parameters, threhold is calculated by group
    """
    def generate_sparsity(self, metrics: Dict) -> Dict[str, Dict[str, Tensor]]:
        masks = {}
        # {group_index: {layer_name: metric}}
        grouped_metrics = {idx: {name: metrics[name] for name in names}
                           for idx, names in self.pruner.generate_module_groups().items()}
        for _, group_metric_dict in grouped_metrics.items():
            threshold, sub_thresholds = self._calculate_threshold(group_metric_dict)
            for name, metric in group_metric_dict.items():
                mask = torch.gt(metric, min(threshold, sub_thresholds[name])).type_as(metric)
                masks[name] = self._expand_mask(name, mask)
                if self.continuous_mask:
                    masks[name]['weight'] *= self.pruner.get_modules_wrapper()[name].weight_mask
        return masks

    def _calculate_threshold(self, group_metric_dict: Dict[str, Tensor]) -> Tuple[float, Dict[str, float]]:
        metric_list = []
        sub_thresholds = {}
        total_weight_num = 0

        temp_wrapper_config = self.pruner.get_modules_wrapper()[list(group_metric_dict.keys())[0]].config
        total_sparsity = temp_wrapper_config['total_sparsity']
        max_sparsity_per_layer = temp_wrapper_config.get('max_sparsity_per_layer', {})

        for name, metric in group_metric_dict.items():
            wrapper = self.pruner.get_modules_wrapper()[name]

            # We assume the metric value are all positive right now.
            if self.continuous_mask:
                metric = metric * self._compress_mask(wrapper.weight_mask)

            layer_weight_num = wrapper.module.weight.data.numel()
            total_weight_num += layer_weight_num
            expend_times = int(layer_weight_num / metric.numel())

            retention_ratio = 1 - max_sparsity_per_layer.get(name, 1)
            retention_numel = math.ceil(retention_ratio * layer_weight_num)
            removed_metric_num = math.ceil(retention_numel / (wrapper.weight_mask.numel() / metric.numel()))
            stay_metric_num = metric.numel() - removed_metric_num
            if stay_metric_num <= 0:
                sub_thresholds[name] = metric.min().item() - 1
                continue
            # Remove the weight parts that must be left
            stay_metric = torch.topk(metric.view(-1), stay_metric_num, largest=False)[0]
            sub_thresholds[name] = stay_metric.max()
            if expend_times > 1:
                stay_metric = stay_metric.expand(int(layer_weight_num / metric.numel()), stay_metric_num).contiguous().view(-1)
            metric_list.append(stay_metric)

        total_prune_num = int(total_sparsity * total_weight_num)
        if total_prune_num == 0:
            threshold = torch.cat(metric_list).min().item() - 1
        else:
            threshold = torch.topk(torch.cat(metric_list).view(-1), total_prune_num, largest=False)[0].max().item()
        return threshold, sub_thresholds


class Conv2dDependencyAwareAllocator(SparsityAllocator):
    """
    A specify allocator for Conv2d with dependency aware.
    """

    def __init__(self, pruner: Pruner, dim: int, dummy_input: Any):
        assert isinstance(dim, int), 'Only support single dim in Conv2dDependencyAwareAllocator.'
        super().__init__(pruner, dim=dim)
        self.dummy_input = dummy_input

    def _get_dependency(self):
        graph = self.pruner.generate_graph(dummy_input=self.dummy_input)
        self.pruner._unwrap_model()
        self.channel_depen = ChannelDependency(model=self.pruner.bound_model, dummy_input=self.dummy_input, traced_model=graph.trace).dependency_sets
        self.group_depen = GroupDependency(model=self.pruner.bound_model, dummy_input=self.dummy_input, traced_model=graph.trace).dependency_sets
        self.pruner._wrap_model()

    def generate_sparsity(self, metrics: Dict) -> Dict[str, Dict[str, Tensor]]:
        self._get_dependency()
        masks = {}
        grouped_metrics = {}
        for idx, names in enumerate(self.channel_depen):
            grouped_metric = {name: metrics[name] for name in names if name in metrics}
            if self.continuous_mask:
                for name, metric in grouped_metric.items():
                    metric *= self._compress_mask(self.pruner.get_modules_wrapper()[name].weight_mask)
            if len(grouped_metric) > 0:
                grouped_metrics[idx] = grouped_metric
        for _, group_metric_dict in grouped_metrics.items():
            group_metric = self._group_metric_calculate(group_metric_dict)

            sparsities = {name: self.pruner.get_modules_wrapper()[name].config['total_sparsity'] for name in group_metric_dict.keys()}
            min_sparsity = min(sparsities.values())

            conv2d_groups = [self.group_depen[name] for name in group_metric_dict.keys()]
            max_conv2d_group = np.lcm.reduce(conv2d_groups)

            pruned_per_conv2d_group = int(group_metric.numel() / max_conv2d_group * min_sparsity)
            conv2d_group_step = int(group_metric.numel() / max_conv2d_group)

            group_mask = []
            for gid in range(max_conv2d_group):
                _start = gid * conv2d_group_step
                _end = (gid + 1) * conv2d_group_step
                if pruned_per_conv2d_group > 0:
                    threshold = torch.topk(group_metric[_start: _end], pruned_per_conv2d_group, largest=False)[0].max()
                    conv2d_group_mask = torch.gt(group_metric[_start:_end], threshold).type_as(group_metric)
                else:
                    conv2d_group_mask = torch.ones(conv2d_group_step, device=group_metric.device)
                group_mask.append(conv2d_group_mask)
            group_mask = torch.cat(group_mask, dim=0)

            for name, metric in group_metric_dict.items():
                # We assume the metric value are all positive right now.
                metric = metric * group_mask
                pruned_num = int(sparsities[name] * len(metric))
                threshold = torch.topk(metric, pruned_num, largest=False)[0].max()
                mask = torch.gt(metric, threshold).type_as(metric)
                masks[name] = self._expand_mask(name, mask)
                if self.continuous_mask:
                    masks[name]['weight'] *= self.pruner.get_modules_wrapper()[name].weight_mask
        return masks

    def _group_metric_calculate(self, group_metrics: Union[Dict[str, Tensor], List[Tensor]]) -> Tensor:
        """
        Add all metric value in the same position in one group.
        """
        group_metrics = list(group_metrics.values()) if isinstance(group_metrics, dict) else group_metrics
        assert all(group_metrics[0].size() == group_metric.size() for group_metric in group_metrics), 'Metrics size do not match.'
        group_sum_metric = torch.zeros(group_metrics[0].size(), device=group_metrics[0].device)
        for group_metric in group_metrics:
            group_sum_metric += group_metric
        return group_sum_metric
