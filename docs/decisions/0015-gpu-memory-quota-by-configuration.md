---
title: GPU 显存额度由部署配置声明
status: accepted
date: 2026-11-21
---

# ADR 0015 GPU 显存额度由部署配置声明

## 背景

开发机 MegaCube 的 GPU 是 GB10，统一内存架构。2026-11-21 实测：

- `nvidia-smi` 与 NVML 不报告设备显存总量（容器内显示 `[N/A]`）
- 因此 NVIDIA device plugin 无法构建设备映射，Pod 申请 `nvidia.com/gpu` 不可用
- 不设限时 `torch` 看到 121.66 GiB；设置 `CUDA_DEVICE_MEMORY_LIMIT=6g` 后看到 6.00 GiB，
  申请 8 GiB 直接被拒绝（`CUDA out of memory … total capacity of 6.00 GiB`）

同一时期定下的方向是：**我们知道这张卡有多大，额度由我们给定，不应依赖硬件上报**。
此前额度写在批准模板的启动参数里（`--gpu-memory-utilization 0.25`），把部署决策
塞进了模板，且与"每台机器的实际显存"脱钩。

## 决定

1. **额度是部署配置。** 引擎显存上限由 `ENGINE_GPU_MEMORY_MIB` 等配置给出，
   不从硬件读取、不写进批准模板。同一模板可在不同机器上得到不同额度。
2. **执行由进程级注入完成。** `libvgpu.so` 经 `LD_PRELOAD` 挂载，配
   `CUDA_DEVICE_MEMORY_LIMIT` 生效；容器与 Pod 走同一条注入路径，不需要设备插件。
3. **不按显存记账做调度。** 需要 GPU 的工作负载通过 `runtimeClassName` 与显式环境变量
   取得设备，不申请 `nvidia.com/gpu`；一台机器上多实例的隔离由额度保证，不由调度器保证。
4. **校验判据是容器内的 `total_memory`。** `torch.cuda.get_device_properties(0).total_memory`
   必须等于配置值；`nvidia-smi` 在统一内存设备上不可作为判据。
5. **模板不得写死比例。** 批准模板出现 `--gpu-memory-utilization` 一类参数即判失败，
   由 `deploy/cluster/check-parity.py` 的规则守住。

## 后果

- 显存额度可在不重建镜像、不改模板的前提下按机器调整。
- 开发环境与生产使用同一注入点，差异只在配置值。
- 依赖 NVML 显存上报的调度或记账方案在该硬件上不可用，需在选择平台侧方案时排除。
- 额度过小会让引擎启动失败，这一失败模式必须显式暴露，不能静默降级到无限制。

## 证据

实测记录见[MegaCube k3s 安装与显存额度](../plans/2026-11-21-megacube-k3s-install.md)
的"显存额度"一节，包括限制前后 `total_memory` 与分配结果的对比。