#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dragon's Crown Pro PC 启动器 - 简体中文界面语言包应用器
========================================================

独立项目：https://github.com/dy55555/dragons-crown-pro-pc-zh-cn
上游项目：https://github.com/saulob/dragons-crown-pro-pc （GPL-2.0）

原理（不改功能，只改显示）
--------------------------
1. 启动器把一份 TSV 翻译表编译进 exe，Tr() 按「英文原文」查表返回译文。
   本脚本只重写这张表的第二列，英文 key 列一字不动，因此所有设置项、
   判断逻辑、存档与联机行为都不变。
2. SetActiveLanguage(key) 只在 key == "brazilian_portuguese" 时打开查表。
   对 Asia/Hong Kong 构建（CUSA10209），该 key 在游戏端 ResolveTextLanguage()
   会回落到原生语言槽，PS4 语言 ID 与游戏内翻译注入都不变，
   所以它只影响启动器 UI，不影响游戏内菜单与字幕。
3. 启动器默认字体 Segoe UI 没有中文字形。字体名被编译器以 SSO 立即数
   内联进 .text 的三条指令里（改 .rdata 里的字符串副本无效），
   本脚本按指令签名做等长替换，换成系统黑体 simhei.ttf。

升级后怎么继续用
----------------
官方更新会覆盖 exe，但不会动本语言包。重新双击「一键汉化.bat」即可：
  - 脚本自动识别「exe 已经被官方换成新版」，更新原版备份再打补丁；
  - 词条按英文原文匹配，不依赖行号/文件偏移，上游增删重排词条不会错位；
  - 上游新增的词条暂时显示英文，往 zh-CN.tsv 补一行译文即可；
  - 若上游换了编译器导致字体指令签名变化，脚本会尝试宽松匹配，
    实在匹配不上会明确报错并且不写入任何文件（不会留下半吊子 exe）。

用法
----
  python apply_zh_cn.py                    # 应用 / 重新应用（幂等）
  python apply_zh_cn.py --check           # 只看状态，不写任何文件
  python apply_zh_cn.py --restore         # 还原官方 exe 与 dcp.ini
  python apply_zh_cn.py --exe PATH        # 指定 exe（也可把 exe 拖到 bat 上）
  python apply_zh_cn.py --version         # 查看语言包与上游版本信息

找 exe 的顺序：--exe / 环境变量 DCP_EXE / config.json / 脚本同级 /
脚本上级 / 常见安装路径。找不到时会给出明确指引。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACK_NAME = 'zh-CN.tsv'
STATE_NAME = 'zh-CN.state.json'
UPSTREAM_NAME = 'upstream.json'
CONFIG_NAME = 'config.json'
ORIG_SUFFIX = '.zhcn-orig'
ACTIVATE_KEY = 'brazilian_portuguese'
DEFAULT_FONT = 'simhei.ttf'
INI_MARK = '# zh-CN UI language pack (apply_zh_cn.py)'

# 在 exe 中定位翻译表的锚点。上游若改了首行，还有几个备用；全失效时走通用扫描。
MARKERS = (
    b'Online version mismatch\t',
    b'The game could not start\t',
    b'Update available\t',
    b'Bug Report\t',
    b'Release notes\t',
)

# 三处内联字体名：movabs imm64 (前 8 字节) + 一段指令 + mov imm32 (末尾 4 字节)。
# imm32 写入的字符串偏移：regular/semibold 是 7（与 imm64 末字节重叠 1 字节），bold 是 8。
FONT_SIGS = (
    ('regular', b'segoeui.', b'\x49\x89\x04\x1c', b'\x41\xc7\x44\x1c\x07', b'.ttf', 7),
    ('semibold', b'seguisb.', b'\x49\x89\x44\x1d\x00', b'\x41\xc7\x44\x1d\x07', b'.ttf', 7),
    ('bold', b'segoeuib', b'\x48\x89\x44\x1d\x00', b'\xc7\x44\x1d\x08', b'.ttf', 8),
)
PREFIX = b'\x48\xb8'
CJK = re.compile(r'[\u4e00-\u9fff]')
ESCAPE_OK = re.compile(r'\\[nst\\]')

DEFAULT_EXE_PATHS = (
    Path(r'D:/GAMES/DragonsCrownPro/dragons_crown_pro.exe'),
    Path(r'C:/GAMES/DragonsCrownPro/dragons_crown_pro.exe'),
    Path(r'D:/Games/DragonsCrownPro/dragons_crown_pro.exe'),
    Path(r'C:/Games/DragonsCrownPro/dragons_crown_pro.exe'),
)


class ApplyError(Exception):
    pass


def log(msg: str = '') -> None:
    try:
        print(msg, flush=True)
    except (OSError, UnicodeError):
        # 输出被管道截断 / 控制台编码异常时，不让提示信息变成一堆 traceback。
        pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def resolve_exe(arg: str | None) -> Path:
    candidates: list[Path] = []
    if arg:
        candidates.append(Path(arg))
    env = os.environ.get('DCP_EXE')
    if env:
        candidates.append(Path(env))
    cfg = load_json(HERE / CONFIG_NAME).get('exe')
    if cfg:
        candidates.append(Path(str(cfg)))
    candidates += [
        HERE / 'dragons_crown_pro.exe',
        HERE.parent / 'dragons_crown_pro.exe',
        *DEFAULT_EXE_PATHS,
    ]
    for item in candidates:
        p = Path(str(item)).expanduser()
        if p.is_file():
            return p.resolve()
    raise ApplyError(
        '找不到 dragons_crown_pro.exe。任选一种方式指定：\n'
        '       1) 把 exe 直接拖到「一键汉化.bat」上；\n'
        f'       2) 在本目录新建 {CONFIG_NAME}，内容 {{"exe": "完整路径"}}；\n'
        '       3) 命令行运行 python apply_zh_cn.py --exe "完整路径"；\n'
        '       4) 把整个语言包文件夹放进游戏目录里。'
    )


def load_pack() -> dict[str, str]:
    path = HERE / PACK_NAME
    if not path.is_file():
        raise ApplyError(f'缺少语言包：{path}')
    pack: dict[str, str] = {}
    bad: list[str] = []
    for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line or line.startswith('#'):
            continue
        if line.count('\t') != 1:
            bad.append(f'{n}: 不是两列')
            continue
        en, zh = line.split('\t')
        if not en or not zh:
            bad.append(f'{n}: 有空列')
            continue
        if en in pack:
            bad.append(f'{n}: 英文 key 重复')
            continue
        pack[en] = zh
    if bad:
        raise ApplyError('语言包格式有问题：\n  ' + '\n  '.join(bad[:10]))
    if len(pack) < 100:
        raise ApplyError(f'语言包只有 {len(pack)} 条，疑似损坏。')
    return pack


def table_quality(text: str) -> tuple[int, int]:
    """返回 (有效词条数, 非空行数)。补齐用的空行不算进比例。"""
    lines = text.split('\n')
    tabbed = sum(1 for l in lines if l.count('\t') == 1)
    nonempty = sum(1 for l in lines if l.strip())
    return tabbed, nonempty


def scan_table_start(data: bytes) -> int:
    """锚点全部失效时的兜底：扫描 NUL 分隔的块，找 tab 两列最密集的那块。"""
    best_start, best_n = -1, 0
    i = 0
    size = len(data)
    while i < size:
        t = data.find(b'\t', i)
        if t < 0:
            break
        s = data.rfind(b'\x00', 0, t) + 1
        e = data.find(b'\x00', t)
        if e < 0:
            break
        block = data[s:e]
        if 1000 <= len(block) <= 4_000_000:
            try:
                text = block.decode('utf-8')
            except UnicodeDecodeError:
                i = e + 1
                continue
            tabbed, nonempty = table_quality(text)
            if tabbed >= 100 and tabbed / max(1, nonempty) >= 0.8 and tabbed > best_n:
                best_start, best_n = s, tabbed
        i = e + 1
    return best_start


def find_table(data: bytes) -> tuple[int, int, list[str]]:
    pos = -1
    for marker in MARKERS:
        pos = data.find(marker)
        if pos >= 0:
            break
    if pos < 0:
        pos = scan_table_start(data)
        if pos < 0:
            raise ApplyError(
                '在 exe 中找不到翻译表。上游可能大改了本地化实现，\n'
                '       请到语言包项目提 issue 附上版本号，本次未写入任何文件。'
            )
    start = data.rfind(b'\x00', 0, pos) + 1
    end = data.find(b'\x00', pos)
    if end < 0:
        raise ApplyError('翻译表没有正常结束（找不到字符串终止符）。')
    try:
        text = data[start:end].decode('utf-8')
    except UnicodeDecodeError as e:
        raise ApplyError(f'翻译表不是合法 UTF-8：{e}') from e
    tabbed, nonempty = table_quality(text)
    if tabbed < 100 or tabbed / max(1, nonempty) < 0.8:
        raise ApplyError(f'翻译表区域异常：只找到 {tabbed} 行有效词条。')
    return start, end, text.split('\n')


def is_patched(data: bytes) -> bool:
    try:
        _, _, lines = find_table(data)
    except ApplyError:
        return False
    hits = sum(1 for l in lines if '\t' in l and CJK.search(l.split('\t', 1)[1]))
    return hits > 50


def valid_escape(text: str) -> bool:
    """TSV 解码器只认 \\n \\s \\t \\\\，其它反斜杠会让整行解析失败。"""
    i = 0
    while i < len(text):
        if text[i] == '\\':
            if i + 1 >= len(text) or not ESCAPE_OK.match(text, i):
                return False
            i += 2
        else:
            i += 1
    return True


def build_table(lines: list[str], pack: dict[str, str]) -> tuple[bytes, dict]:
    out: list[str] = []
    stats = dict(total=0, hit=0, miss=0, skipped=[])
    for line in lines:
        if line.count('\t') != 1 or not line.strip():
            out.append(line)
            continue
        en, _old = line.split('\t')
        stats['total'] += 1
        zh = pack.get(en)
        if zh is None:
            # 上游新增词条：宁可显示英文，也不要漏出官方葡语。
            out.append(en + '\t' + en)
            stats['miss'] += 1
            continue
        if zh.count('%s') != en.count('%s'):
            out.append(en + '\t' + en)
            stats['skipped'].append(f'{en[:48]} (%s 数量不符)')
            continue
        if not valid_escape(zh):
            out.append(en + '\t' + en)
            stats['skipped'].append(f'{en[:48]} (非法转义)')
            continue
        out.append(en + '\t' + zh)
        stats['hit'] += 1
    return '\n'.join(out).encode('utf-8'), stats


def font_patch_pair(sig, font: str) -> tuple[bytes, bytes]:
    _tag, imm64_old, mid1, mid2, imm32_old, offset = sig
    old_name = imm64_old[:offset] + imm32_old
    name = font.encode('ascii')
    if len(name) > len(old_name) - 1:
        raise ApplyError(f'字体名 {font} 太长，放不进 {len(old_name)} 字节的槽位。')
    new_name = (name + b'\x00' * len(old_name))[:len(old_name)]
    imm64_new = new_name[:8]
    imm32_new = new_name[offset:offset + 4]
    if len(imm64_new) != 8 or len(imm32_new) != 4:
        raise ApplyError('字体槽位构造失败。')
    old_sig = PREFIX + imm64_old + mid1 + mid2 + imm32_old
    new_sig = PREFIX + imm64_new + mid1 + mid2 + imm32_new
    assert len(old_sig) == len(new_sig)
    return old_sig, new_sig


def patch_fonts(data: bytearray, font: str) -> list[str]:
    report = []
    for sig in FONT_SIGS:
        tag = sig[0]
        old_sig, new_sig = font_patch_pair(sig, font)
        if data.count(new_sig) == 1:
            report.append(f'{tag}: 已是目标字体，跳过')
            continue
        n = data.count(old_sig)
        if n == 1:
            i = data.find(old_sig)
            data[i:i + len(old_sig)] = new_sig
            report.append(f'{tag}: 精确签名替换 @ 0x{i:x}')
            continue
        # 上游换了编译器时的宽松兜底：只换 movabs 的 8 字节和紧随的 .ttf。
        head = PREFIX + sig[1]
        p = data.find(head)
        if p < 0 or n != 0:
            raise ApplyError(
                f'{tag}: 找不到字体名指令签名（匹配数 {n}）。\n'
                '       上游可能换了编译器或字体加载方式，需要重新适配；本次未写入任何文件。'
            )
        q = data.find(b'.ttf', p + len(head), p + len(head) + 24)
        if q < 0:
            raise ApplyError(f'{tag}: 宽松匹配也没找到 .ttf 常量，未写入任何文件。')
        data[p + 2:p + 10] = new_sig[2:10]
        data[q:q + 4] = new_sig[-4:]
        report.append(f'{tag}: 宽松签名替换 @ 0x{p:x}（请留意界面是否正常）')
    return report


def read_state() -> dict:
    return load_json(HERE / STATE_NAME)


def write_state(state: dict) -> None:
    (HERE / STATE_NAME).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def ini_path_for(exe: Path) -> Path:
    return exe.parent / 'user' / 'dcp.ini'


def apply_ini(ini: Path, state: dict) -> str:
    if not ini.is_file():
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_text('', encoding='utf-8')
    lines = ini.read_text(encoding='utf-8').splitlines()
    idx = next((i for i, l in enumerate(lines)
                if l.strip().startswith('text_language') and '=' in l), None)
    before = lines[idx].split('=', 1)[1].strip() if idx is not None else None
    if 'ini_text_language_before' not in state:
        state['ini_text_language_before'] = before
    if idx is not None:
        lines[idx] = f'text_language = {ACTIVATE_KEY}'
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines += ['', INI_MARK, f'text_language = {ACTIVATE_KEY}']
    if not any(l.strip() == INI_MARK for l in lines):
        at = next((i for i, l in enumerate(lines)
                   if l.strip().startswith('text_language')), len(lines))
        lines.insert(at, INI_MARK)
    ini.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return f'text_language = {ACTIVATE_KEY}' + (f'（原值：{before}）' if before else '（原无此行）')


def restore_ini(ini: Path, state: dict) -> str:
    if not ini.is_file():
        return 'dcp.ini 不存在，跳过'
    lines = ini.read_text(encoding='utf-8').splitlines()
    lines = [l for l in lines if l.strip() != INI_MARK]
    before = state.get('ini_text_language_before', 'MISSING')
    idx = next((i for i, l in enumerate(lines)
                if l.strip().startswith('text_language') and '=' in l), None)
    if before == 'MISSING' or before is None:
        if idx is not None:
            lines.pop(idx)
            while lines and not lines[-1].strip():
                lines.pop()
            msg = '已移除 text_language 行'
        else:
            msg = '无需改动'
    else:
        if idx is not None:
            lines[idx] = f'text_language = {before}'
        else:
            lines += ['', f'text_language = {before}']
        msg = f'text_language 已还原为 {before}'
    ini.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return msg


def write_exe(exe: Path, data: bytes) -> None:
    try:
        exe.write_bytes(data)
    except PermissionError as e:
        raise ApplyError(
            f'无法写入 {exe.name}：文件正被占用。\n'
            '       请先关闭启动器 / 游戏，再重新运行本脚本。'
        ) from e


def upstream_line() -> str:
    meta = load_json(HERE / UPSTREAM_NAME)
    if not meta:
        return ''
    ver = meta.get('based_on_version', '?')
    commit = str(meta.get('based_on_commit', ''))[:8]
    return f'  适配上游   : {ver}' + (f' ({commit})' if commit else '')


def cmd_check(exe: Path, orig: Path, pack: dict) -> None:
    data = exe.read_bytes()
    log(f'  exe        : {exe}')
    log(f'  SHA-256    : {sha256(data)[:16]}…  ({len(data):,} 字节)')
    log(f'  当前状态   : {"已汉化" if is_patched(data) else "官方原版/未汉化"}')
    log(f'  原版备份   : {orig if orig.is_file() else "无"}')
    if upstream_line():
        log(upstream_line())
    try:
        s, e, lines = find_table(data)
        keys = [l.split('\t', 1)[0] for l in lines if l.count('\t') == 1]
        hit = sum(1 for k in keys if k in pack)
        blanks = len(lines) - len(keys)
        extra = f' + {blanks} 行补齐空白' if blanks else ''
        log(f'  翻译表     : 0x{s:x}..0x{e:x}，{e - s:,} 字节，{len(keys)} 条词条{extra}')
        log(f'  词条覆盖   : {hit}/{len(keys)}（语言包共 {len(pack)} 条）')
        if hit < len(keys):
            log('              上游有新增词条，显示为英文；往 zh-CN.tsv 补一行即可。')
    except ApplyError as err:
        log(f'  翻译表     : {err}')
    for sig in FONT_SIGS:
        old, new = font_patch_pair(sig, DEFAULT_FONT)
        log(f'  字体 {sig[0]:<9}: 官方签名 {data.count(old)} 处 / 目标签名 {data.count(new)} 处')
    log()


def cmd_apply(exe: Path, orig: Path, pack: dict, font: str) -> None:
    current = exe.read_bytes()
    state = read_state()
    patched = is_patched(current)

    if patched and orig.is_file():
        base = orig.read_bytes()
        base_note = '基于原版备份重新生成（幂等）'
    elif patched:
        base = current
        base_note = '当前已是汉化版且无原版备份，就地重算'
    else:
        base = current
        if not orig.is_file() or sha256(orig.read_bytes()) != sha256(current):
            orig.write_bytes(current)
            base_note = '检测到官方新版本，已更新原版备份'
        else:
            base_note = '官方原版（备份已存在）'

    log(f'[1/5] 基础文件：{base_note}')
    log(f'      原版备份：{orig}')

    data = bytearray(base)
    s, e, lines = find_table(bytes(data))
    tabbed = sum(1 for l in lines if l.count('\t') == 1)
    log(f'[2/5] 定位翻译表：0x{s:x}..0x{e:x}，{e - s:,} 字节，{tabbed} 条词条')

    table, stats = build_table(lines, pack)
    budget = e - s
    if len(table) > budget:
        raise ApplyError(
            f'译文表 {len(table):,} 字节，超出可用空间 {budget:,} 字节。\n'
            '       请精简 zh-CN.tsv 里较长的译文，本次未写入任何文件。'
        )
    padded = table + b'\n' * (budget - len(table))
    data[s:e] = padded
    log(f'[3/5] 写入中文：命中 {stats["hit"]}/{stats["total"]} 条，'
        f'未收录 {stats["miss"]} 条（显示英文），占用 {len(table):,}/{budget:,} 字节')
    if stats['skipped']:
        log(f'      跳过异常译文 {len(stats["skipped"])} 条：')
        for item in stats['skipped'][:5]:
            log(f'        - {item}')

    for line in patch_fonts(data, font):
        log(f'[4/5] 字体 {line}')

    # 全部构造成功才落盘，避免半吊子状态。
    ini = ini_path_for(exe)
    write_exe(exe, bytes(data))
    ini_note = apply_ini(ini, state)
    state.update({
        'exe': str(exe),
        'base_sha256': sha256(base),
        'patched_sha256': sha256(bytes(data)),
        'entries': stats['hit'],
        'font': font,
    })
    write_state(state)
    log(f'[5/5] 激活查表：{ini_note}')
    log()
    log('完成。启动 dragons_crown_pro.exe 即可看到简体中文界面。')
    log('官方升级后：重新双击「一键汉化.bat」即可。')
    log(f'还原：python "{Path(__file__).name}" --restore')


def cmd_restore(exe: Path, orig: Path) -> None:
    state = read_state()
    if not orig.is_file():
        raise ApplyError(f'没有找到原版备份 {orig}，无法还原。')
    write_exe(exe, orig.read_bytes())
    log(f'已还原官方 exe：{exe}')
    log(f'dcp.ini：{restore_ini(ini_path_for(exe), state)}')
    state.pop('patched_sha256', None)
    state.pop('ini_text_language_before', None)
    write_state(state)
    log('完成。')


def cmd_version() -> None:
    pack = load_pack()
    meta = load_json(HERE / UPSTREAM_NAME)
    log('简体中文界面语言包')
    log(f'  词条数：{len(pack)}')
    for key, label in (('upstream', '上游项目'), ('upstream_license', '上游许可'),
                       ('based_on_version', '适配版本'), ('based_on_commit', '适配提交'),
                       ('license', '本项目许可')):
        if meta.get(key):
            log(f'  {label}：{meta[key]}')
    log()


def main() -> int:
    ap = argparse.ArgumentParser(description='Dragon\'s Crown Pro PC 简体中文界面语言包应用器')
    ap.add_argument('--exe', help='dragons_crown_pro.exe 完整路径')
    ap.add_argument('--font', default=DEFAULT_FONT, help=f'界面字体文件名（默认 {DEFAULT_FONT}）')
    ap.add_argument('--check', action='store_true', help='只检查状态，不写文件')
    ap.add_argument('--restore', action='store_true', help='还原官方 exe 与 dcp.ini')
    ap.add_argument('--version', action='store_true', help='显示语言包与上游版本信息')
    args = ap.parse_args()

    if args.version:
        try:
            cmd_version()
        except ApplyError as err:
            log(f'[失败] {err}')
            return 1
        return 0

    log('=' * 62)
    log(' Dragon\'s Crown Pro PC  ·  简体中文界面语言包')
    log('=' * 62)
    try:
        exe = resolve_exe(args.exe)
        orig = exe.with_name(exe.name + ORIG_SUFFIX)
        if args.check:
            cmd_check(exe, orig, load_pack())
        elif args.restore:
            cmd_restore(exe, orig)
        else:
            cmd_apply(exe, orig, load_pack(), args.font)
    except ApplyError as err:
        log()
        log(f'[失败] {err}')
        return 1
    except OSError as err:
        log()
        log(f'[失败] 文件操作出错：{err}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
