# 场景工程开发与验证记录

基于已有视频分支 `a82e966`，开发分支 `feat/scene-project-sync`。保留原 TTS、音色/主题、音频模式、SRT 和 GPL 授权；没有增加 AI 创作、图片搜索/理解或模型 API。

## 阶段 1：配置记忆与错误诊断

- `global_settings.py` 在 AppData 原子保存工具路径、默认参数、输出目录、最近工程和配音设置。
- 选择工具实际运行 `-version`，文件夹不会作为程序执行。配置按当前编辑、工程、全局、程序默认合并。
- 原 `settings.json` 也改为原子写入，保留已有键和声音设置。
- 输出可写性/剩余磁盘检查、WinError 5 明确诊断、完整 FFmpeg 错误日志与场景编号。
- 回归时复现损坏图片在 `-loop 1` 下永久重试，加入 `-xerror` 后实际故障测试能够立即退出并保留完整日志。

## 阶段 2：统一输入与精确配音

- 导入目录读取脚本及 JSON，按字符串 ID 映射并按 JSON 顺序显示；缺图保留场景，提示替换或显式黑画面占位。
- 逐场景调用原 TTSEngine，正常请求间隔 1.5 秒，限流退避/重试复用原引擎；缓存校验 SHA256 和真实 WAV 样本数。
- PCM 累计样本偏移生成全局场景时间、SRT 和 timeline JSON，不按图片平均分配，不依赖旧句末数量上限。
- 支持严格文本对应的既有音频/SRT复用、取消续跑、顺序调整后缓存重拼、参数变化后选择性重生成。
- JSON 时间和锁定编辑保留，冲突明确阻止导出而非裁配音；resolved 结果不覆盖原始输入。

## 阶段 3：GUI、媒体测试与交付

Linux/Python 3.12 下完整 **113 项测试通过，零跳过**。另运行实际 GUI 取消按钮回归并通过。测试命令：

```bash
DISPLAY=:97 APPDATA=/workspace/edge-tts-environment/scene-tests SDL_AUDIODRIVER=dummy \
  .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
git diff --check
```

实际媒体验证包括：

- 11 场景目录、JSON/脚本/图片一一对应，附加元数据保留。
- 10/20/50 段已知真实 PCM 样本累计；一张图片与一段或多个完整句子的解说对应。
- 实际 50 图片 FFmpeg MP4，黑画面占位仍保留配音及字幕，FFprobe 验证总时长（不累计漂移，固定 FPS 允许约一帧量化差异）。
- 既有音频/SRT 无 TTS 请求直接导出、缓存续跑、重新排序、变更语速后的重生成与未变参数的复用。
- 实际窗口导入 11 场景、工具验证和保存、后台配音/导出、缺图跳过、工程/设置恢复和实际取消按钮。
- 损坏图片快速失败，场景编号/命令参数/完整日志；WinError 5 启动和输出目录错误路径。

网络 TTS 在自动化测试中使用已知 PCM 固定素材替代。没有把模拟音频当作 Microsoft 在线服务的验证，也没有使用大模型做强制对齐。真实服务质量、429 恢复和目标电脑行为需手工验收。

Windows PR CI 沿用原流程：安装依赖与 FFmpeg，运行完整测试，构建并实际启动 onedir/onefile，再上传 EXE、许可证、中文指南和输入示例。执行结果以 PR 的 Windows 检查为准；不创建软件 Release、不合并主分支。

目标 Windows 11 还需验证实际用户路径权限、安全软件拦截、中文字体/播放设备、RTX 3060 NVENC、长时 1080×1920 高清素材性能，以及 Inno Setup 安装/卸载。操作步骤见 `SCENE_PROJECT_GUIDE.zh-CN.md`。
