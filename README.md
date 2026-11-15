# Dragon's Crown Pro PC · 简体中文界面语言包

给 [saulob/dragons-crown-pro-pc](https://github.com/saulob/dragons-crown-pro-pc) 的 TRG 启动器界面做简体中文汉化。
**不改任何功能逻辑、不含游戏本体、不含上游 exe。官方升级后重新双击一次 bat 就能继续用。**

---

## 上游项目 / Upstream

| 项目 | 内容 |
| --- | --- |
| 上游仓库 | **[saulob/dragons-crown-pro-pc](https://github.com/saulob/dragons-crown-pro-pc)** |
| 上游作者 | [saulob](https://github.com/saulob) |
| 适配版本 | `v1.2.0` · commit `fc6acdb` · 2026-11-13 |
| 上游许可证 | GPL-2.0 |
| 本项目许可证 | GPL-2.0（译文属于上游 UI 文本的演绎，沿用同一许可证） |
| 机器可读引用 | [`upstream.json`](upstream.json) |

本仓库是**独立的非官方语言包**，不是上游的 fork，也不隶属于上游作者。
游戏安装、PKG 解包、联机、金手指等一切说明，请以上游 README 为准。
本项目不提供、也不包含任何游戏文件；你需要自己合法拥有的《龙之皇冠 Pro》PS4 版本。

---

## 一键汉化

1. 安装 Python 3（Windows 安装时勾选 *Add Python to PATH*）。
2. 把本文件夹放到任意位置；**推荐直接放进游戏安装目录**（和 `dragons_crown_pro.exe` 同级或下一级）。
3. 双击 **`一键汉化.bat`**。
4. 正常启动 `dragons_crown_pro.exe`，界面即为简体中文。

如果脚本找不到 exe，任选一种：

| 方式 | 操作 |
| --- | --- |
| 拖放（最省事） | 把 `dragons_crown_pro.exe` 直接拖到 `一键汉化.bat` 图标上 |
| 配置文件 | 把 `config.example.json` 复制为 `config.json`，改成你的实际路径 |
| 命令行 | `python apply_zh_cn.py --exe "完整路径"` |
| 环境变量 | 设置 `DCP_EXE` 为 exe 完整路径 |

> 运行前请先关闭启动器 / 游戏，否则 exe 被占用会写入失败（脚本会明确提示）。

---

## 官方升级后继续用（重点）

| 情况 | 你要做的 |
| --- | --- |
| 官方更新覆盖了 exe | **重新双击 `一键汉化.bat`**，一次搞定，无需重新下载语言包 |
| 更新后多了新设置项 | 新词条先显示英文；往 `zh-CN.tsv` 补一行译文，再跑一次 bat |
| 想确认当前是不是汉化版 | 双击 `检查状态.bat` |
| 想回官方原版 | 双击 `还原官方版.bat` |

升级后还能用，靠的是这三点：

- **按英文原文查表**，不按行号、不按文件偏移。上游增删词条、重排顺序都不会错位。
- 脚本自动识别「exe 已被官方换成新版」，更新原版备份后重新打补丁；重复运行完全幂等。
- 翻译表锚点全失效时会扫描 exe 找两列表；字体指令变了会尝试宽松匹配；
  实在不兼容会**明确报错且不写入任何文件**，不会留下半个坏掉的 exe。

---

## 改翻译 / 补词条

`zh-CN.tsv` 每行两列，用 TAB 分隔：

```
英文原文<TAB>简体中文译文
```

第一列是查表用的 key，**不要改**；只改第二列。规则：

- 转义：`\s` = 空格，`\n` = 换行，`\t` = 制表符，`\\` = 反斜杠。
- `%s` 等占位符必须和英文原文**数量一致**，否则该行会被跳过并显示英文。
- 以 `#` 开头的是注释行。
- 未收录的词条一律回退为英文，不会串出上游自带的葡语。

改完重新双击 `一键汉化.bat` 即可生效。想贡献译文欢迎提 PR。

---

## 不影响哪些东西

整套补丁只动三处，全在「显示层」：

| 改动 | 作用 |
| --- | --- |
| exe 内 TSV 翻译表的**第二列** | 启动器界面文字换成中文；英文 key 列一字不动 |
| `.text` 中三处内联字体名 | Segoe UI → `simhei.ttf`，让中文有字形 |
| `user/dcp.ini` 增加 `text_language = brazilian_portuguese` | 打开启动器自带的 `Tr()` 查表开关 |

**不改**：游戏内菜单与字幕、存档、联机 / 好友码、奖杯、金手指逻辑与数值、设置项的实际取值、任何游戏文件（CPK / PKG / eboot）。

对 Asia/Hong Kong 构建（`CUSA10209`），这个语言 key 在游戏端会回落到原生语言槽，
PS4 语言 ID 与游戏内翻译注入都不变 —— 所以它只作用于启动器 UI。源码依据：
`launcher/game_profile.cpp` 的 `kAsiaLanguages`、`launcher/settings.cpp` 的
`dcp_text_language()` / `dcp_text_language_is_portuguese_brazil()`。

> ⚠️ 启动器「菜单与字幕语言」下拉里只有本构建自带的语言。
> 别在那个下拉里改选项，会覆盖掉汉化开关；真改了就重新跑一次 bat。

---

## 工作原理（简版）

上游 v1.2.0 启动器内置了一套本地化机制：`Tr()` / `TrValue()` 拿英文原文去查一张
编译进 exe 的 TSV 表，`SetActiveLanguage(key)` 决定要不要启用查表。官方目前只带
`Portuguese (Brazil)` 一张表。本语言包做的就是：**保留英文 key 列，把译文列换成简体中文**，
再让这个开关打开。

中文显示还有一道坎：启动器字体由 TRG Launcher 的 `LoadSystemFonts()` 从
`C:\Windows\Fonts\` 加载 `segoeui.ttf` 等 Segoe UI 字体，没有中文字形，直接换 TSV 会全是方块。
更麻烦的是字体名被编译器以 SSO 立即数内联进 `.text` 的三条指令里
（`movabs imm64` + `mov imm32`），改 `.rdata` 里的字符串副本**没有效果**。
所以脚本按指令签名做等长替换，把三处字体名换成系统黑体 `simhei.ttf`。
不需要动 ImGui 的 glyph range —— 实测换字体后中文即可正常渲染。

表区尾部多出来的空间用空行补齐（`ParseUiTextTable()` 会跳过空行），
因此不需要改动任何文件长度或 PE 结构。

---

## 已验证环境

| 项 | 值 |
| --- | --- |
| 上游版本 | v1.2.0 |
| 游戏构建 | `CUSA10209 1.00 Asia/Hong Kong` |
| 系统 / Python | Windows · Python 3.13 |
| 词条覆盖 | 736 / 736 |
| 实测项目 | 主界面、图形 / 金手指 / 奖杯页切换、幂等重跑、还原闭环、模拟上游改版后重新适配 |

其它构建（美版 `CUSA10454`、日版 `CUSA07001`、欧版 `CUSA10487`）理论上同样走 `Tr()`，
但**未实测**。特别注意：美版的语言列表里真的包含 `Portuguese (Brazil)`，
在这些构建上该 key 会启用上游的葡语游戏内翻译，不再只是启动器开关。
换构建前请先跑 `检查状态.bat` 并自行确认，必要时提 issue 一起适配。

---

## 文件说明

| 文件 | 用途 |
| --- | --- |
| `zh-CN.tsv` | 736 条简体中文译文，语言包本体 |
| `apply_zh_cn.py` | 打补丁 / 检查 / 还原的应用脚本 |
| `一键汉化.bat` | 双击入口（支持把 exe 拖上来） |
| `检查状态.bat` | 只看状态，不写任何文件 |
| `还原官方版.bat` | 还原官方 exe 与 `dcp.ini` |
| `config.example.json` | 自定义游戏路径的模板 |
| `upstream.json` | 上游项目与适配版本的机器可读引用 |
| `LICENSE` | GPL-2.0 |
| `*.zhcn-orig` | 自动生成在**游戏目录**的官方 exe 备份（不上传） |
| `zh-CN.state.json` | 本地状态记录，用于精确还原（不上传） |

---

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 中文是方块 / 菱形 | 字体没换成功，跑 `检查状态.bat` 看三处字体签名 |
| 提示「文件正被占用」 | 关掉启动器 / 游戏再跑 |
| 界面还是英文 | 跑 `检查状态.bat`，确认「当前状态」和 `dcp.ini` 里的 `text_language` |
| 官方大改版后脚本报错 | 提 issue，附上启动器版本号和完整报错；脚本此时不会写坏 exe |
| 升级后个别项仍是英文 | 上游新增词条，往 `zh-CN.tsv` 补一行再跑一次 |

---

## 许可证与免责

本项目按 **GPL-2.0** 发布，见 [`LICENSE`](LICENSE)；上游项目同为 GPL-2.0。

本项目与 Vanillaware、Atlus、Sony Interactive Entertainment 及上游作者均无关联，
是非官方的界面翻译补丁。不含游戏本体，也不含上游可执行文件；
请仅对你自己合法获得的 PS4 版本使用。使用风险自负。
