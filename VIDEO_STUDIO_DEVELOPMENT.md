# 分阶段实现与验证记录

基线：`92efdb5`（上游 v1.7.2）。功能分支：`feat/image-video-studio`。
没有重写原配音引擎；保留 GPL 许可、作者信息和原有音频模式。

| 阶段 | 交付 | 验证 |
|---|---|---|
| 1 | 顶部三页导航、共享 Project、保留音频布局 | 原 75 项测试通过 |
| 2 | SRT 解析/编码/朗读文本、图片管理、自然排序、JSON 相对路径 | 补装新直接依赖 Pillow 后输入测试通过 |
| 3 | 完整句末 DP、时长约束、锁定边界、单图/不足点处理、FFmpeg 分段渲染 | 40 秒 5 图等规划测试通过 |
| 4 | TTS 持久副本、真实时间轴、ASS 字幕、配音/BGM、原子输出与取消 | 真实媒体流水线测试通过 |
| 5 | 图片导入/目录/拖放、排序编辑、字幕关联、吸附、参数表单、工程重开、低分辨率预览 | 实际 Xorg 窗口操作通过 |
| 6 | 中文指南、Windows 启动/打包脚本、PR 构建 CI、EXE 冒烟入口 | Linux 下完整 93 项测试通过，零跳过；Windows 待 CI |

## 实际执行证据

Linux/Python 3.12，已有 FFmpeg/FFprobe，虚拟 Xorg 显示器。

```bash
DISPLAY=:98 APPDATA=/workspace/edge-tts-environment/home SDL_AUDIODRIVER=dummy \
  .venv/bin/python -m unittest discover -s tests -v
# Ran 93 tests ... OK（包括原 75 项、真实媒体和 GUI）
.venv/bin/python -m pip check
# No broken requirements found.
git diff --check
```

媒体测试自动生成两张图片、两秒 WAV、中文 SRT，实际执行：字幕烧录、配音+BGM、
H.264/AAC、MP4 封装、无声导出、外部视频合成、取消保护，以及 24 种适配/运动组合。
使用 FFprobe 验证流、编码和总时长；解码输出帧验证红/蓝图片按顺序切换。
NVENC 请求在本机不可用时实际走 CPU 回退；不据此宣称 NVIDIA 硬件编码成功。

GUI 回归：创建实际窗口，导入 BOM SRT、写入原音频输入框、添加图片、规划、保存/重开、
跨页参数保留、主题/语言切换、返回原生成按钮。另测默认与 1000×620 窗口下表格仍可见。
发现并修复“尚无音频时错误弹出字幕时长差异提示”和视频面板挤压图片表格的问题。

## 当前边界与验收事项

- 核心离线视频流程已验证。在线 Microsoft TTS 沿用原引擎，此轮未发送在线合成请求；
  没有把 mock 或本地合成音频当成微软在线合成成功。
- 首版预览采用临时低分辨率 MP4 + 系统播放器；主界面提供静态图片/字幕时间检查，
  不宣称内置完整实时多轨播放器。
- 首版 fade 是渐暗/渐亮，不是两张图片的交叉叠化；严格句末默认 cut。
- 新视频表单为中文；已有音频功能的语言切换仍保留。
- FFmpeg/FFprobe 单独安装或配置，不自动下载、不绕过校验，不强制 GPU。
- Linux 无法产出或验收 Windows EXE。Windows PR CI 定义了 Python 测试、实际 FFmpeg 测试、
  onedir/onefile 构建及两个 EXE 的真实启动检查。必须查看该工作流结果后再判断 Windows 包是否通过。
- 仍需目标 Windows 11 电脑验收：中文字体实际效果、文件拖放、声卡播放、系统播放器关联、
  NVENC 硬件分支、长时 1080×1920 大批量素材的性能、Inno Setup 安装/卸载。
- 未发布云环境版本，也未创建软件 Release。

补充打包检查：Linux PyInstaller onedir 已实际启动并完成三个工作区的
`--smoke-test`，返回 `{"status":"passed","workspaces":3}`。
该云 Python 自带 Tcl/Tk 9，首次冻结启动缺少 `libtcl9.0.so`/`libtcl9tk9.0.so`；
使用 PyInstaller 支持的 `--add-binary <实际库路径>:.` 显式收集这两个库后通过。
这属于本机 Linux 工具链差异，不把 Linux 库路径写入 Windows 构建脚本。
