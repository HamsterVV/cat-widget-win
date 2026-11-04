# 桌面宠物（橘猫）

一只用真实照片 + AI 视频生成的 Windows 桌面宠物：沿着屏幕底部慢慢走，会喝水、舔爪子、按时间段睡觉，左键点它还会转过头看你。整套素材来自同一张参考照片，所以换宠物时身份、机位、比例都是连贯的。

## 运行

双击 `start.bat`。

需要 `python-embed/`（Python 3.12 嵌入式包 + Pillow + Tk），该目录体积大、不含在本仓库里，需要单独放回项目根目录。手动启动：

```bat
python-embed\pythonw.exe cat_widget.py
```

默认全天显示。把 `actions.json` 里 `layout.deadline` 改成 `"17:00"` 这类时间，就恢复"到点走出右边缘并退出"的作息；改回 `null` 则全天显示。

## 交互

| 操作 | 效果 |
|---|---|
| 左键单击 | 停下，转头看向屏幕约 3 秒，然后继续走 |
| 按住拖动 | 把宠物拖到任意位置 |
| 右键 | 打开设置面板 |

## 行为

- 默认一直在走路，从屏幕左侧 8% 起步；没有 deadline 时缓慢向右漂移，不会走出屏幕
- 喝水：按设定间隔提醒（默认 15 分钟）
- 舔爪：走路结束后按概率随机触发
- 睡觉：在设定的时间段内一直趴着，到点自己起来
- 动作之间 0.15–0.3 秒交叉淡入，不会硬切

## 设置面板

右键打开，可调：喝水间隔/时长、舔爪时长与概率、睡觉时间段、大小、切换淡入。保存后面板自动关闭并立即生效。

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
tools/                 素材管线（不参与运行）
```

---

# 用 AI 换一只宠物

整条路线是：**一张参考照片定身份 → AI 视频生成动作 → 抽帧 → 抠像归一化 → 装进项目**。所有动作都用同一张首帧生成，所以是同一个小动物、同一机位、同一比例。

## 0. 需要什么

- 一张宠物照片：侧身站立、四肢完整、背景干净（稍后要垫成纯绿）
- 阿里云 Token Plan 的 API key（生视频用 `wan3.0-video`）
- 本仓库的 `tools/` 脚本和 `python-embed/`

## 1. 准备参考图

把照片抠图后垫在**纯绿色 `#00FF00`** 上，导出 PNG。要求：

- 宠物侧身站立，头朝左（项目默认这套朝向，朝右会自动翻转）
- 全身完整，耳朵到爪子都在画面内，四周留一点空白
- 画面比例接近 16:9，主体高度占画面 80% 以上

把成品命名为 `my_pet_green.png` 放在项目根目录。**这张图就是身份基准**，后面每个动作都以它为首帧。

## 2. 配置 API key

密钥不要写进仓库。二选一：

```bat
set TOKEN_PLAN_API_KEY=你的密钥
```

或写到 `C:\Users\<用户名>\.aliyun-tokenplan.key`（文件里只有一行密钥）。

## 3. 生成动作视频

每个动作生成一条 5 秒绿幕视频，**首帧都用同一张参考图**：

```bat
python-embed\python.exe tools\aliyun_video.py ^
  --first-frame my_pet_green.png ^
  --prompt "保持第一帧这只橘猫的外形、毛色、花纹、体型、拍摄角度和画面占比完全不变。纯绿色背景 #00FF00，固定机位，全身侧面视角。动作：..." ^
  --negative "换一只猫,改变毛色,改变花纹,改变体型,身体转动,镜头移动,镜头缩放,背景变化,非纯绿背景,文字,水印,人物,其他动物" ^
  --duration 5 --resolution 720P --out assets\frames_raw\walk.mp4
```

动作提示词要点（把"猫"换成你的宠物）：

| 动作 | 提示词里要写清楚 | 备注 |
|---|---|---|
| walk | 匀速向前走，四肢交替，身体保持侧面 | 这是基准动作，先做它 |
| drink | 低头舔水三四次，再抬头回到站姿 | 首尾都是站姿，方便切换 |
| lickpaw | 坐下 → 抬前爪舔 → 放下 → 站起 | 首尾都是站姿 |
| sleep | 趴下蜷成一团，闭眼，腹部随呼吸起伏 | 时长靠 `tail_loop` 续 |
| look | **身体、腿、尾巴完全不动，只有脖子和头转向镜头**，看三秒再转回去 | 不写死会连身体一起转，侧面轮廓变短、看起来变小 |

`--submit-only` 只提交任务并打印 `task_id`，之后可以用 `--task-id <id>` 续查和下载，避免重复扣费。

## 4. 抽帧、抠像、验收

```bat
REM 抽帧（无头 Chrome，10fps）
python-embed\python.exe tools\capture_frames.py --video assets\frames_raw\walk.mp4 --out assets\frames_staging\walk_frames --fps 10

REM 抠像 + 归一化（生成到 assets\frames\walk）
python-embed\python.exe tools\ingest_frames.py assets\frames\walk assets\frames_staging\walk_frames\*.png ^
  --height 384 --canvas-height 384 ^
  --scale-ref assets\frames_staging\walk_frames\walk_00.png ^
  --match-color-to assets\frames\walk\walk_08.png

REM 验收：边缘、半透明、几何
python-embed\python.exe tools\check_frames.py assets\frames\walk
```

几个参数的作用：

- `--scale-ref`：用这条片子的**第一帧侧站姿**算出一个缩放系数，整条片子共用。不传的话每帧会按自己的包围盒缩放，坐下/趴下会被放大成和站着一样高
- `--canvas-height 384`：每帧贴到 384 高的画布上并**底边对齐**，保证脚踩同一条地面线
- `--match-color-to`：把毛色均值对齐到已有动作的某一帧，避免不同批次的色温不一致
- `check_frames` 要全部 PASS（绿边、半透明雾、几何）

## 5. 装进项目

在 `actions.json` 里加动作：

```json
"lickpaw": {
  "frames_dir": "assets/frames/lickpaw",
  "fps": 10,
  "loop": false,
  "weight": 20,
  "mode": "random",
  "duration_s": [4.7, 4.7]
}
```

| 字段 | 含义 |
|---|---|
| `fps` | 抽帧帧率，和 `capture_frames --fps` 保持一致 |
| `loop` | 走路用 `true`；一次性动作（喝、舔、睡、看人）用 `false` |
| `duration_s` | 单次持续秒数，**要等于 帧数 ÷ fps**，否则会定格在最后一帧 |
| `weight` | 随机动作的权重，概率 = `w / (100 + 全部权重)` |
| `tail_loop` | 一次性片段的尾部循环帧数，趴下后循环呼吸用 |
| `move_px_per_loop` | 每个循环前进的像素，走路才有 |

镜像不需要单独存目录：加载时按方向翻转。

## 6. 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 动作结束前定格几秒 | `duration_s` 大于 帧数÷fps，改小；一次性动作也不会继承上一段的相位了 |
| 某个动作看起来变小 | 生成时身体跟着转了，侧面投影变短。重生成时强调"身体不动，只有头转"，或统一按内容高度归一化 |
| 脚下悬空 / 比别的动作高 | 没加 `--canvas-height` 或首帧没踩到画布底边 |
| 毛边有绿边 | 抠像阈值问题，调 `ingest_frames.py` 的背景噪声估计；`check_frames` 会报 `rim`/`haze` |
| 同一只宠物色温不一致 | 加 `--match-color-to` 对齐毛色 |

## 已踩过的坑

- 分层窗口必须在 `CreateWindowEx` 时带 `WS_EX_LAYERED`，且进程必须真的 DPI 感知
- 嵌入式 Python 的 `_pth` 不把脚本目录放进 `sys.path`，需要手动插入，否则会加载同名的旧模块
- 窗口类不设 `hCursor` 时系统会显示 WAIT 光标
- 在鼠标消息回调里做重活（加载几十帧 + 改窗口尺寸）会让进程静默退出，必须丢给渲染循环
- 一次性动作不能继承循环相位，否则重复触发时会从最后一帧开始、整段定格