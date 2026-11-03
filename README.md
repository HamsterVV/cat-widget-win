# 橘猫桌面宠物

一只用真实照片素材做出来的 Windows 桌面宠物：橘猫沿着屏幕底部慢慢往右走，会喝水、舔爪子、按时间段睡觉，左键点它还会转过头看你。

## 运行

双击 `start.bat`。需要 `python-embed/`（Python 3.12 嵌入式包 + Pillow + Tk），该目录体积大、不含在本仓库中，需要单独放回项目根目录。

手动启动：

```bat
python-embed\pythonw.exe cat_widget.py
```

17:00 之后启动会直接退出（当天已经收工），需要继续跑就改 `actions.json` 里的 `layout.deadline`。

## 交互

| 操作 | 效果 |
|---|---|
| 左键单击 | 停下，转头看向屏幕约 3 秒，然后继续走 |
| 按住拖动 | 把猫拖到任意位置 |
| 右键 | 打开设置面板 |

## 行为

- 默认一直在走路，从屏幕左侧 8% 起步，17:00 走出右边缘并退出
- 喝水：按设定间隔提醒（默认 15 分钟）
- 舔爪：走路结束后按概率随机触发
- 睡觉：在设定的时间段内一直趴着，到点自己起来
- 动作之间是 0.15–0.3 秒交叉淡入，不会硬切

## 目录

```
cat_widget.py          入口：装日志、开 DPI 感知、起窗口
pet/
  config.py            常量、设置校验、日志、DPI、路径
  scheduler.py         时段与事件的调度器
  frames.py            帧加载、躯干质心对齐、缓存
  ui.py                气泡、自绘控件、设置面板
  app.py               动作状态机、渲染循环、移动、交互
  window.py            Win32 分层窗口渲染器（真 alpha）
actions.json           动作定义（帧目录、fps、循环、时长、权重）
settings.json          用户设置（提醒、睡觉时段、大小、淡入）
assets/frames/         运行用精灵帧（walk / drink / lickpaw / sleep / look）
tools/                 素材管线与版本工具（不参与运行）
```

## 设置面板

右键打开，可调：喝水间隔/时长、舔爪时长与概率、睡觉时间段、大小、切换淡入。保存后面板自动关闭并立即生效。

## 素材管线

`tools/` 里的脚本只用于重新生成素材，运行宠物不需要它们：

```bat
python-embed\python.exe tools\capture_frames.py --video clip.mp4 --out frames --fps 10
python-embed\python.exe tools\ingest_frames.py out_dir frames\*.png --height 384 --scale-ref frames\f00.png --canvas-height 384
python-embed\python.exe tools\check_frames.py out_dir
python-embed\python.exe tools\snapshot.py v2.3 "说明"
```

- 所有动作都来自同一只猫的同一张参考照片，用 `wan3.0-video` 生成绿幕视频后抽帧抠像，保证身份、机位、比例一致
- 每帧按统一比例归一化并底边对齐，坐下、趴下、转头时大小不变
- 素材统一 384px 高、二值 alpha；朝右显示时在加载阶段翻转，不再单独存一份镜像目录

## 已踩过的坑

- 分层窗口必须在 `CreateWindowEx` 时带 `WS_EX_LAYERED`，且进程必须真的 DPI 感知
- 嵌入式 Python 的 `_pth` 不把脚本目录放进 `sys.path`，需要手动插入，否则会加载同名的旧模块
- 窗口类不设 `hCursor` 时系统会显示 WAIT 光标
- 在鼠标消息回调里做重活（加载几十帧 + 改窗口尺寸）会让进程静默退出，必须丢给渲染循环