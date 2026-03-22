"""
kif2usi.py — KIF表記 → USIムーブ変換 + 全局面SFEN取得

使用法:
  from kif2usi import kif_to_usi_moves, get_all_sfens
  moves = kif_to_usi_moves('game.kif')
  sfens = get_all_sfens(moves, engine_path, eval_dir)
"""

import re
import subprocess
import time
from pathlib import Path

# ============================================================
# 変換テーブル
# ============================================================
_FILE_MAP  = {'１':1,'２':2,'３':3,'４':4,'５':5,'６':6,'７':7,'８':8,'９':9}
_RANK_MAP  = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
_RANK_CHAR = {1:'a',2:'b',3:'c',4:'d',5:'e',6:'f',7:'g',8:'h',9:'i'}
_PIECE_MAP = {
    '歩':'P','香':'L','桂':'N','銀':'S','金':'G','角':'B','飛':'R',
    '玉':'K','王':'K','と':'P','成香':'L','成桂':'N','成銀':'S',
    '馬':'B','龍':'R','竜':'R',
}

def _sq(col: int, row: int) -> str:
    return f"{col}{_RANK_CHAR[row]}"


# ============================================================
# KIF生行 → USIムーブ変換
# ============================================================

def kif_line_to_usi(raw_line: str, prev_to: str | None = None) -> tuple[str, str | None, str | None]:
    """
    KIF生行 → (move_jp, usi_move, new_prev_to)

    raw_line例:
      '1 ７六歩(77) (00:00/00:00:00)'
      '32 同　銀(43) (00:00/00:00:00)'
      '45 ４四歩打 (00:00/00:00:00)'
      '53 ３三銀成(44) (00:00/00:00:00)'
    """
    raw_line = raw_line.rstrip()

    # 投了・終局系
    if '投了' in raw_line or '中断' in raw_line or '千日手' in raw_line:
        return '投了', None, prev_to

    # 手番行マッチ
    m = re.match(r'^\s*\d+\s+(.+)', raw_line)
    if not m:
        return None, None, prev_to

    rest = m.group(1)  # '７六歩(77) (00:00/...)' など

    # move_jp の抽出（括弧 or 半角スペースの手前まで）
    jp_m = re.match(r'(.+?)\s*(?:\(\d\d\)|打|\s)', rest)
    move_jp = (jp_m.group(1).strip() if jp_m else rest.split()[0])
    move_jp = move_jp.replace('同　', '同').replace('同 ', '同')

    # ── 打ち駒 ──────────────────────────────
    if '打' in rest:
        to_m = re.match(r'([１-９])([一二三四五六七八九])', move_jp)
        if not to_m:
            return move_jp, None, prev_to
        to_sq = _sq(_FILE_MAP[to_m.group(1)], _RANK_MAP[to_m.group(2)])
        piece = next((p for k, p in _PIECE_MAP.items() if k in move_jp), None)
        usi = f"{piece}*{to_sq}" if piece else None
        return move_jp, usi, to_sq

    # ── 通常手・成り ──────────────────────────
    # 移動元 (77) を rest から取る
    from_m = re.search(r'\((\d)(\d)\)', rest)
    if not from_m:
        return move_jp, None, prev_to
    from_sq = _sq(int(from_m.group(1)), int(from_m.group(2)))

    # 移動先
    if move_jp.startswith('同'):
        to_sq = prev_to
    else:
        to_m = re.match(r'([１-９])([一二三四五六七八九])', move_jp)
        if not to_m:
            return move_jp, None, prev_to
        c = _FILE_MAP.get(to_m.group(1))
        r = _RANK_MAP.get(to_m.group(2))
        if c is None or r is None:
            return move_jp, None, prev_to
        to_sq = _sq(c, r)

    if to_sq is None:
        return move_jp, None, prev_to

    promote = '+' if ('成' in move_jp and 'と' not in move_jp) else ''
    return move_jp, f"{from_sq}{to_sq}{promote}", to_sq


# ============================================================
# KIFファイル全体 → USIムーブリスト
# ============================================================

def kif_to_usi_moves(kif_path: str) -> list[dict]:
    """
    KIFファイルを読んで各手の情報を返す。
    戻り値: [{'num': 1, 'move_jp': '７六歩', 'usi_move': '7g7f'}, ...]
    """
    results = []
    in_moves = False
    prev_to = None

    with open(kif_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if line.rstrip().startswith('手数'):
                in_moves = True
                continue
            if not in_moves:
                continue

            m = re.match(r'^\s*(\d+)\s+', line)
            if not m:
                continue

            num = int(m.group(1))
            move_jp, usi, new_prev = kif_line_to_usi(line, prev_to)

            if move_jp == '投了':
                results.append({'num': num, 'move_jp': '投了', 'usi_move': None})
                break
            if move_jp:
                prev_to = new_prev
                results.append({'num': num, 'move_jp': move_jp, 'usi_move': usi})

    return results


# ============================================================
# やねうら王で全局面のSFENを取得
# ============================================================

def get_all_sfens(usi_moves: list[dict], engine_path: str, eval_dir: str) -> list[str | None]:
    """
    各手を指す直前の局面SFENをやねうら王経由で取得。
    sfens[i] = usi_moves[i] を指す前の局面SFEN。

    注意: EvalDir を setoption より先に渡さないとエンジンが落ちる。
    """
    proc = subprocess.Popen(
        [engine_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )

    def send(cmd: str):
        proc.stdin.write(cmd + '\n')
        proc.stdin.flush()

    def wait_for(kw: str, timeout: float = 15.0) -> str:
        start = time.time()
        while time.time() - start < timeout:
            line = proc.stdout.readline()
            if kw in line:
                return line
        return ''

    def read_sfen(timeout: float = 5.0) -> str | None:
        start = time.time()
        while time.time() - start < timeout:
            line = proc.stdout.readline()
            if line.strip().startswith('sfen '):
                return line.strip()[5:].strip()
        return None

    # EvalDir を先に設定してから usi/isready
    send(f'setoption name EvalDir value {eval_dir}')
    send('usi')
    wait_for('usiok')
    send('isready')
    wait_for('readyok', timeout=30)

    valid_moves = [m['usi_move'] for m in usi_moves if m['usi_move']]
    sfens: list[str | None] = []

    for i, move in enumerate(usi_moves):
        if move['move_jp'] == '投了':
            break

        moves_so_far = valid_moves[:i]
        pos_cmd = 'position startpos' + (f" moves {' '.join(moves_so_far)}" if moves_so_far else '')
        send(pos_cmd)
        send('d')
        sfens.append(read_sfen())

    send('quit')
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    return sfens


# ============================================================
# テスト
# ============================================================

if __name__ == '__main__':
    import sys

    KIF  = sys.argv[1] if len(sys.argv) > 1 else \
        '/Users/hiroki/Library/CloudStorage/Dropbox/Scripts/shogi_ai/kiri_site/nakahara_ohyama_1992.kif'
    ENG  = '/Users/hiroki/Library/CloudStorage/Dropbox/Scripts/shogi_ai/engines/YaneuraOu/source/YaneuraOu-by-gcc'
    EVAL = '/Users/hiroki/shogi/eval'

    print('=== KIF → USI変換 ===')
    moves = kif_to_usi_moves(KIF)
    ok = sum(1 for m in moves if m['usi_move'])
    ng = sum(1 for m in moves if not m['usi_move'] and m['move_jp'] != '投了')
    print(f'  成功: {ok}手 / 失敗: {ng}手 / 合計: {len(moves)}手')
    for m in moves[:10]:
        print(f"  {m['num']:3d}手 {m['move_jp']:12s} → {m['usi_move'] or '(投了)'}")

    if Path(ENG).exists():
        print('\n=== SFEN取得テスト（最初の5局面） ===')
        sfens = get_all_sfens(moves[:6], ENG, EVAL)
        for i, (m, s) in enumerate(zip(moves[:5], sfens[:5])):
            tag = s[:70] + '...' if s else '(取得失敗)'
            print(f"  {m['num']}手目前: {tag}")
    else:
        print(f'\n⚠ エンジン未検出: {ENG}')
