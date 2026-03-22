#!/usr/bin/env python3
"""
generate.py — Kiri 将棋解析サイト生成スクリプト
KIF → 4エンジン解析 → JSON → HTML

使用法:
  python generate.py nakahara_ohyama_1992.kif
  python generate.py *.kif          # 複数一括
  python generate.py --json-only nakahara_ohyama_1992.kif   # JSON生成のみ
"""

import os, sys, re, json, subprocess, time, argparse, shutil, string
from pathlib import Path
from datetime import datetime

# ============================================================
# パス設定
# ============================================================
BASE_DIR    = Path(__file__).parent
DOCS_DIR    = BASE_DIR / "docs"
GAMES_DIR   = DOCS_DIR / "games"
SHOGI_DIR   = Path("/Users/hiroki/shogi")
ENGINE_PATH = Path("/Users/hiroki/Library/CloudStorage/Dropbox/Scripts/shogi_ai/engines/YaneuraOu/source/YaneuraOu-by-gcc")

EVAL_DIRS = {
    "水匠5":          str(SHOGI_DIR / "eval"),
    "tanuki":         str(SHOGI_DIR / "eval_tanuki"),
    "Kristallweizen": str(SHOGI_DIR / "eval_kristallweizen"),
}

TEMPLATE_PATH = BASE_DIR / "template.html"

# ============================================================
# KIF パーサー
# ============================================================
def parse_kif(kif_path: Path) -> dict:
    """KIFファイルをパースしてメタ情報と手順を返す"""
    meta = {}
    moves = []
    in_moves = False

    with open(kif_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip()

            # メタ情報
            for key in ["開始日時", "棋戦", "場所", "先手", "後手", "戦型", "手合割"]:
                if line.startswith(key + "："):
                    meta[key] = line.split("：", 1)[1]

            # 手順開始
            if line.startswith("手数"):
                in_moves = True
                continue

            if not in_moves:
                continue

            # 投了・中断
            if "投了" in line or "中断" in line or "千日手" in line:
                m = re.match(r'^\s*(\d+)', line)
                if m:
                    moves.append({"num": int(m.group(1)), "move_jp": "投了", "sfen_move": None})
                break

            # 指し手行: "  1 ７六歩(77) ..."
            m = re.match(r'^\s*(\d+)\s+(.+?)\s*\(', line)
            if m:
                num = int(m.group(1))
                move_jp = m.group(2).strip()
                # "同　銀" などの正規化
                move_jp = move_jp.replace("同　", "同")
                moves.append({"num": num, "move_jp": move_jp, "sfen_move": None})

    return {"meta": meta, "moves": moves}


# ============================================================
# KIF → SFEN 変換（やねうら王を使う）
# ============================================================
def kif_to_sfens(kif_path: Path, engine_path: Path) -> list[str]:
    """やねうら王にKIFを食わせて全手のSFENリストを得る"""
    sfens = []
    proc = subprocess.Popen(
        [str(engine_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    def send(cmd):
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()

    def wait_for(keyword, timeout=10):
        start = time.time()
        while time.time() - start < timeout:
            line = proc.stdout.readline()
            if keyword in line:
                return line
        return ""

    send("usi")
    wait_for("usiok")
    send("isready")
    wait_for("readyok")

    # KIFの手順をUSIムーブに変換するため、
    # 一手ずつ "position ... moves ..." で送り、
    # "d" コマンドでSFENを取得する
    with open(kif_path, encoding="utf-8", errors="replace") as f:
        kif_text = f.read()

    # やねうら王の kif2sfen 機能を使う
    send(f"kif2sfen {kif_text.replace(chr(10), ' ')}")
    # kif2sfen が使えない場合は手動変換にフォールバック
    # ここでは position startpos を積み上げる方式
    # TODO: 実エンジン連携後に実装を確定

    send("quit")
    proc.wait()
    return sfens


# ============================================================
# エンジン評価（USIプロトコル）
# ============================================================
class USIEngine:
    def __init__(self, engine_path: Path, eval_dir: str, threads: int = 4, hash_mb: int = 256):
        self.engine_path = str(engine_path)
        self.eval_dir = eval_dir
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [self.engine_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        # EvalDir は usi より前に設定（やねうら王の要件）
        self._send(f"setoption name EvalDir value {self.eval_dir}")
        self._send("usi")
        self._wait_for("usiok")
        self._send("setoption name Threads value 4")
        self._send("setoption name USI_Hash value 256")
        self._send("isready")
        self._wait_for("readyok", timeout=30)

    def _send(self, cmd: str):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _wait_for(self, keyword: str, timeout: int = 15) -> str:
        start = time.time()
        while time.time() - start < timeout:
            line = self.proc.stdout.readline()
            if keyword in line:
                return line
        return ""

    def evaluate(self, sfen: str, depth: int = 12) -> dict:
        """指定SFENをdepthまで探索して評価値を返す"""
        self._send(f"position sfen {sfen}")
        return self._go_and_parse(depth)

    def evaluate_pos(self, pos_cmd: str, depth: int = 12) -> dict:
        """position コマンド文字列をそのまま送って評価"""
        self._send(pos_cmd)
        return self._go_and_parse(depth)

    def _go_and_parse(self, depth: int) -> dict:
        """go depth で探索して評価値を返す（やねうら王は常に先手視点）"""
        self._send(f"go depth {depth}")

        score = None
        best_move = None
        start = time.time()

        while time.time() - start < 60:
            line = self.proc.stdout.readline().strip()
            if line.startswith("bestmove"):
                best_move = line.split()[1] if len(line.split()) > 1 else None
                break
            if line.startswith("info") and "score" in line:
                s = re.search(r'score cp (-?\d+)', line)
                if s:
                    score = int(s.group(1))
                mate = re.search(r'score mate (-?\d+)', line)
                if mate:
                    n = int(mate.group(1))
                    score = 30000 if n > 0 else -30000

        return {"score": score, "best_move": best_move}

    def evaluate_multipv(self, pos_cmd: str, depth: int = 12, multipv: int = 5) -> dict:
        """MultiPVで探索して評価値＋上位候補手を返す"""
        self._send(f"setoption name MultiPV value {multipv}")
        self._send(pos_cmd)
        self._send(f"go depth {depth}")

        best_score = None
        best_move = None
        multipv_lines = []
        start = time.time()

        while time.time() - start < 60:
            line = self.proc.stdout.readline().strip()
            if line.startswith("bestmove"):
                best_move = line.split()[1] if len(line.split()) > 1 else None
                break
            if line.startswith("info") and "multipv" in line and "score cp" in line:
                multipv_lines.append(line)
            elif line.startswith("info") and "score cp" in line and "multipv" not in line:
                s = re.search(r'score cp (-?\d+)', line)
                if s:
                    best_score = int(s.group(1))
            elif line.startswith("info") and "score mate" in line:
                mate = re.search(r'score mate (-?\d+)', line)
                if mate:
                    n = int(mate.group(1))
                    best_score = 30000 if n > 0 else -30000

        sys.path.insert(0, str(Path("/Users/hiroki/shogi")))
        from kiri_engine import parse_multipv
        candidates = parse_multipv(multipv_lines)

        if not candidates and best_move:
            candidates = [{"rank": 1, "score": best_score or 0, "move": best_move, "pv": [best_move]}]

        # MultiPV後にシングルPVに戻す
        self._send("setoption name MultiPV value 1")

        return {"score": best_score or (candidates[0]["score"] if candidates else None),
                "best_move": best_move,
                "candidates": candidates}

    def quit(self):
        if self.proc:
            self._send("quit")
            self.proc.wait(timeout=5)


class FukauraOuEngine:
    """ふかうら王（dlshogi CoreML）専用エンジンクラス"""

    BINARY = "/Users/hiroki/shogi/fukauraou/YaneuraOu-fukauraou"
    MODEL  = "/Users/hiroki/shogi/fukauraou/eval/DlShogiResnet10SwishBatch.mlmodel"

    def __init__(self):
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            [self.BINARY],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self._send(f"setoption name DNN_Model1 value {self.MODEL}")
        self._send("setoption name DNN_Batch_Size1 value 1")
        self._send("usi")
        self._wait_for("usiok", timeout=10)
        self._send("isready")
        self._wait_for("readyok", timeout=60)  # モデルコンパイルに時間がかかる

    def _send(self, cmd: str):
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _wait_for(self, keyword: str, timeout: int = 30) -> str:
        start = time.time()
        while time.time() - start < timeout:
            line = self.proc.stdout.readline()
            if keyword in line:
                return line
        return ""

    def evaluate_multipv(self, pos_cmd: str, depth: int = 12, multipv: int = 5) -> dict:
        """MultiPVで探索して評価値＋候補手を返す（NNUEと同じインターフェース）"""
        self._send(f"setoption name MultiPV value {multipv}")
        self._send(pos_cmd)
        self._send(f"go depth {depth}")

        best_score = None
        best_move = None
        multipv_lines = []
        start = time.time()

        while time.time() - start < 120:
            line = self.proc.stdout.readline().strip()
            if line.startswith("bestmove"):
                best_move = line.split()[1] if len(line.split()) > 1 else None
                break
            if line.startswith("info") and "multipv" in line and "score cp" in line:
                multipv_lines.append(line)
            elif line.startswith("info") and "score cp" in line and "multipv" not in line:
                s = re.search(r'score cp (-?\d+)', line)
                if s:
                    best_score = int(s.group(1))
            elif line.startswith("info") and "score mate" in line:
                mate = re.search(r'score mate (-?\d+)', line)
                if mate:
                    n = int(mate.group(1))
                    best_score = 30000 if n > 0 else -30000

        sys.path.insert(0, str(Path("/Users/hiroki/shogi")))
        from kiri_engine import parse_multipv
        candidates = parse_multipv(multipv_lines)

        if not candidates and best_move:
            candidates = [{"rank": 1, "score": best_score or 0,
                           "move": best_move, "pv": [best_move]}]

        return {
            "score": best_score or (candidates[0]["score"] if candidates else None),
            "best_move": best_move,
            "candidates": candidates,
        }

    def quit(self):
        if self.proc:
            self._send("quit")
            self.proc.wait(timeout=10)


# ============================================================
# USI → 日本語変換
# ============================================================
def _usi_to_japanese(usi_move: str, board) -> str:
    """USI手を shogi.Board 状態で日本語表記に変換"""
    try:
        import shogi
        move = shogi.Move.from_usi(usi_move)
        if move.drop_piece_type:
            to_file = 9 - shogi.file_index(move.to_square)
            to_rank = shogi.rank_index(move.to_square) + 1
            piece_name = shogi.PIECE_JAPANESE_SYMBOLS[move.drop_piece_type]
            file_jp = shogi.NUMBER_JAPANESE_NUMBER_SYMBOLS[to_file]
            rank_jp = shogi.NUMBER_JAPANESE_KANJI_SYMBOLS[to_rank]
            return f'{file_jp}{rank_jp}{piece_name}打'
        else:
            to_file = 9 - shogi.file_index(move.to_square)
            to_rank = shogi.rank_index(move.to_square) + 1
            file_jp = shogi.NUMBER_JAPANESE_NUMBER_SYMBOLS[to_file]
            rank_jp = shogi.NUMBER_JAPANESE_KANJI_SYMBOLS[to_rank]
            piece = board.piece_at(move.from_square)
            piece_name = shogi.PIECE_JAPANESE_SYMBOLS[piece.piece_type] if piece else '?'
            promote_str = '成' if move.promotion else ''
            from_file = 9 - shogi.file_index(move.from_square)
            from_rank = shogi.rank_index(move.from_square) + 1
            return f'{file_jp}{rank_jp}{piece_name}{promote_str}({from_file}{from_rank})'
    except Exception:
        return usi_move


def _convert_pv_to_japanese(pv_moves: list, board_copy, max_moves: int = 6) -> list:
    """PV手順リストを日本語に変換（最大max_moves手）"""
    import shogi
    result = []
    import copy
    b = copy.deepcopy(board_copy)
    for usi in pv_moves[:max_moves]:
        jp = _usi_to_japanese(usi, b)
        result.append(jp)
        try:
            b.push(shogi.Move.from_usi(usi))
        except Exception:
            break
    result.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(result):
        r["rank"] = i + 1
    return result


def _build_best_moves(candidates: list, is_gote: bool, board_copy) -> list:
    """候補手リストを JSON出力用に整形"""
    result = []
    for r in candidates:
        score = r["score"]
        if is_gote:
            score = -score
        jp = _usi_to_japanese(r["move"], board_copy)
        pv_jp = _convert_pv_to_japanese(r["pv"], board_copy)
        result.append({
            "rank": r["rank"],
            "move_usi": r["move"],
            "move_jp": jp,
            "score": score,
            "pv_jp": pv_jp,
        })
    result.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(result):
        r["rank"] = i + 1
    return result


# ============================================================
# 全手解析
# ============================================================
def analyze_all_moves(moves: list, engine_path: Path, depth: int = 12) -> list:
    """
    全手をdepthで全エンジン解析する。
    kif2usi.py の kif_to_usi_moves / get_all_sfens を使って
    SFEN一括取得 → 2エンジン評価。
    """
    import shogi
    from kif2usi import kif_to_usi_moves

    print("  KIF → USI変換中...")
    usi_moves = kif_to_usi_moves(str(kif_path_global))
    ok = sum(1 for m in usi_moves if m['usi_move'])
    print(f"  USI変換: {ok}手成功 / {len(usi_moves)}手")

    # 各手を指した後の局面を position startpos moves ... で評価する
    # USIムーブの累積リストを構築
    valid_usi = [m['usi_move'] for m in usi_moves if m['usi_move']]
    moves = usi_moves

    # 2エンジンで解析
    results = []
    engines = {}
    for name, eval_dir in EVAL_DIRS.items():
        if not Path(eval_dir).joinpath("nn.bin").exists():
            print(f"  [{name}] nn.bin 見つからず。スキップ。")
            continue
        print(f"  [{name}] エンジン起動...")
        e = USIEngine(engine_path, eval_dir)
        e.start()
        engines[name] = e

    # ふかうら王を追加
    fukaura_binary = Path("/Users/hiroki/shogi/fukauraou/YaneuraOu-fukauraou")
    fukaura_model  = Path("/Users/hiroki/shogi/fukauraou/eval/DlShogiResnet10SwishBatch.mlmodel")
    if fukaura_binary.exists() and fukaura_model.exists():
        print("  [dlshogi] ふかうら王 起動...")
        fe = FukauraOuEngine()
        fe.start()
        engines["dlshogi"] = fe
        print("  [dlshogi] 起動完了")
    else:
        print("  [dlshogi] バイナリまたはモデルが見つかりません。スキップ。")

    if not engines:
        print("  有効なエンジンがありません。ダミーデータで続行。")
        return _dummy_analysis(moves)

    total = sum(1 for m in moves if m["move_jp"] != "投了")
    for i, move in enumerate(moves):
        if move["move_jp"] == "投了":
            break

        # position startpos moves m0 ... m(i-1)（指す前の局面）
        moves_so_far = valid_usi[:i]
        if moves_so_far:
            pos_cmd = "position startpos moves " + " ".join(moves_so_far)
        else:
            pos_cmd = "position startpos"
        row = {
            "num": move["num"],
            "move_jp": move["move_jp"],
        }

        # shogi.Board を同期させて候補手の日本語変換に使う
        board = shogi.Board()
        for m in moves_so_far:
            try:
                board.push(shogi.Move.from_usi(m))
            except Exception:
                pass

        is_gote = len(moves_so_far) % 2 == 1
        engine_name_key = {
            "水匠5": "best_moves_mizusho",
            "tanuki": "best_moves_tanuki",
            "Kristallweizen": "best_moves_kristallweizen",
            "dlshogi": "best_moves_dlshogi",
        }

        for name, eng in engines.items():
            print(f"  [{name}] {move['num']}/{total}手目 解析中 (depth {depth})...", end="\r")
            res = eng.evaluate_multipv(pos_cmd, depth=depth, multipv=5)
            row[name] = res["score"]
            row[engine_name_key[name]] = _build_best_moves(res["candidates"], is_gote, board)

        # 先手視点に統一: エンジンは手番視点(side-to-move)で返すので
        # 後手番局面(moves_so_far が奇数個)は符号反転
        for key in engines:
            if row.get(key) is not None and is_gote:
                row[key] = -row[key]

        # 勝率変換（先手視点のスコアから計算）
        raw = row.get("水匠5")
        if raw is None:
            raw = row.get("tanuki")
        if raw is None:
            raw = 0
        row["winrate"] = _score_to_winrate(raw)

        results.append(row)

    print()
    for eng in engines.values():
        eng.quit()

    return results


def _get_sfens_manual(moves: list, engine_path: Path) -> list:
    """position startpos moves で積み上げてSFENを列挙"""
    sfens = []
    proc = subprocess.Popen(
        [str(engine_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, bufsize=1,
    )

    def send(cmd):
        proc.stdin.write(cmd + "\n")
        proc.stdin.flush()

    def read_until(keyword, timeout=10):
        start = time.time()
        buf = []
        while time.time() - start < timeout:
            line = proc.stdout.readline()
            buf.append(line)
            if keyword in line:
                return buf
        return buf

    send("usi")
    read_until("usiok")
    send("isready")
    read_until("readyok", timeout=20)

    # 各手のSFENを取得：d コマンドで現在局面を出力
    usi_moves = []
    for move in moves:
        if move.get("usi_move"):
            usi_moves.append(move["usi_move"])

        pos_cmd = "position startpos" + (f" moves {' '.join(usi_moves)}" if usi_moves else "")
        send(pos_cmd)
        send("d")

        lines = read_until("sfen", timeout=3)
        for line in lines:
            m = re.search(r'sfen (.+)', line)
            if m:
                sfens.append(m.group(1).strip())
                break

    send("quit")
    proc.wait()
    return sfens


def _dummy_analysis(moves: list) -> list:
    """エンジンなし時のダミーデータ"""
    import math, random
    results = []
    score = 0
    for move in moves:
        if move["move_jp"] == "投了":
            break
        score += random.randint(-50, 80) if move["num"] % 2 == 1 else random.randint(-80, 50)
        score = max(-3000, min(3000, score))
        noise = random.randint(-30, 30)
        results.append({
            "num": move["num"],
            "move_jp": move["move_jp"],
            "水匠5": score,
            "tanuki": score + noise,
            "winrate": _score_to_winrate(score),
        })
    return results


def _score_to_winrate(score: int) -> float:
    """評価値 → 勝率（%）"""
    import math
    if score is None:
        return 50.0
    return round(100 / (1 + math.exp(-score / 600)), 1)


# ============================================================
# HTML テンプレート生成 (string.Template)
# ============================================================
# string.Template uses $var for substitution.
# Literal $ in JS template literals must be doubled: $$
# JS braces {} do NOT need escaping (unlike str.format).
GAME_HTML_TEMPLATE = string.Template(r'''<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>$sente vs $gote $year — Kiri</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;1,300&family=JetBrains+Mono:wght@300;400&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="../assets/style.css">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    #board-wrap { display:flex; gap:1.2rem; align-items:flex-start; justify-content:center; }
    #board-svg  { flex-shrink:0; }
    #move-counter { font-family:var(--font-mono); font-size:0.68rem; color:var(--text-secondary);
      letter-spacing:0.15em; margin-bottom:0.5rem; text-align:center; min-height:1.2em; }
    #move-nav { display:flex; gap:0.35rem; justify-content:center; margin-top:0.6rem; }
    .nav-btn { background:var(--bg); border:1px solid var(--border); color:var(--text-secondary);
      font-family:var(--font-mono); font-size:0.72rem; padding:0.28rem 0.6rem; cursor:pointer;
      letter-spacing:0.08em; transition:color .2s,border-color .2s; }
    .nav-btn:hover { color:var(--accent-gold); border-color:var(--accent-gold); }
    #captured-wrap { font-family:var(--font-mono); font-size:0.68rem; color:var(--text-secondary); min-width:72px; }
    .cap-label { color:var(--text-muted); font-size:0.58rem; letter-spacing:0.2em; margin-bottom:0.3rem; }
    .cap-pieces { line-height:1.9; min-height:1.4rem; }
    #allMoves tr.highlight-move td { background:rgba(200,169,110,0.1) !important; }
    #candidate-panel { font-family:var(--font-mono); margin-top:1rem; background:var(--bg-card); border:1px solid var(--border); overflow:hidden; }
    #cand-header-bar { padding:0.6rem 1.2rem; background:rgba(255,255,255,0.03); border-bottom:1px solid var(--border); font-size:0.7rem; color:var(--text-muted); letter-spacing:0.15em; }
    #cand-winrate-section { padding:0.8rem 1.2rem 0.6rem; border-bottom:1px solid rgba(255,255,255,0.05); }
    .winrate-row { display:flex; align-items:center; gap:0.6rem; margin-bottom:0.4rem; }
    .engine-label { font-size:0.68rem; width:48px; flex-shrink:0; }
    .bar-container { flex:1; height:1.3rem; background:rgba(255,255,255,0.06); border-radius:2px; overflow:hidden; position:relative; }
    .win-bar { height:100%; transition:width 0.35s ease; }
    .bar-text { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:0.7rem; color:rgba(255,255,255,0.9); }
    #divergence-line { font-size:0.68rem; color:var(--text-muted); text-align:right; margin-top:0.2rem; }
    #cand-grid { display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:0; }
    .cand-col { padding:0.7rem 0.8rem; }
    .cand-col:not(:last-child) { border-right:1px solid rgba(255,255,255,0.05); }
    .cand-engine-label { font-size:0.55rem; letter-spacing:0.15em; margin-bottom:0.5rem; padding-bottom:0.3rem; border-bottom:1px solid rgba(255,255,255,0.05); }
    .cand-item { cursor:pointer; border-radius:2px; transition:background 0.12s; margin-bottom:0.1rem; }
    .cand-item:hover { background:rgba(255,255,255,0.06); }
    .cand-item.active { background:rgba(200,169,110,0.1); }
    .cand-header { display:flex; align-items:baseline; gap:0.35rem; padding:0.25rem 0.4rem; font-size:0.75rem; line-height:1.6; }
    .cand-rank { color:var(--text-muted); font-size:0.65rem; width:14px; flex-shrink:0; }
    .cand-move { flex:1; color:var(--text-primary); }
    .cand-item:not(:first-child) .cand-move { color:var(--text-secondary); }
    .cand-score { color:var(--accent-gold); font-size:0.75rem; }
    .pv-toggle { color:var(--text-muted); font-size:0.58rem; margin-left:2px; }
    .pv-line { display:none; padding:0.2rem 0.4rem 0.5rem 1.4rem; font-size:0.62rem; color:var(--text-secondary); line-height:1.8; }
    .pv-line.open { display:block; }
    .pv-move { display:inline-block; margin-right:0.3rem; margin-bottom:0.1rem; padding:0.05rem 0.25rem; background:rgba(255,255,255,0.04); border-radius:2px; }
    @media (max-width: 900px) {
      #cand-grid { grid-template-columns: 1fr 1fr; }
    }
  </style>
</head>
<body>

<nav>
  <a href="../index.html" class="nav-logo">
    <span class="nav-logo-kiri">Kiri</span>
    <span class="nav-logo-jp">霧 — 将棋解析</span>
  </a>
  <ul class="nav-links">
    <li><a href="../index.html#games">Games</a></li>
    <li><a href="../index.html#archive">Archive</a></li>
    <li><a href="../index.html#about">About</a></li>
  </ul>
</nav>

<div class="game-hero">
  <div class="game-hero-bg"></div>
  <div class="game-hero-overlay"></div>
  <div class="game-hero-content">
    <p style="font-family:var(--font-mono);font-size:0.65rem;letter-spacing:0.25em;color:var(--accent-gold);margin-bottom:0.8rem;">$kisen $year</p>
    <h1 style="font-family:var(--font-serif-jp);font-size:clamp(1.6rem,3vw,2.8rem);font-weight:300;margin-bottom:0.5rem;">
      $sente <span style="color:var(--text-muted);font-size:0.6em;letter-spacing:0.2em;">VS</span> $gote
    </h1>
    <p style="font-family:var(--font-mono);font-size:0.75rem;letter-spacing:0.1em;color:var(--text-secondary);">
      $date ／ $kisen ／ $sentype ／ ${total_moves}手
    </p>
  </div>
</div>

<div class="game-main">
  <div>
    <div class="chart-container">
      <p class="chart-title">棋譜再生 — Board Viewer</p>
      <div id="board-wrap">
        <div>
          <div id="move-counter">初期局面</div>
          <svg id="board-svg" width="396" height="396" viewBox="0 0 396 396" xmlns="http://www.w3.org/2000/svg"></svg>
          <div id="move-nav">
            <button class="nav-btn" onclick="boardGoto(0)">|◁</button>
            <button class="nav-btn" onclick="boardStep(-1)">◁</button>
            <button class="nav-btn" onclick="boardStep(1)">▷</button>
            <button class="nav-btn" onclick="boardGoto(positions.length-1)">▷|</button>
          </div>
        </div>
        <div id="captured-wrap">
          <div class="cap-label">後手持駒</div>
          <div class="cap-pieces" id="cap-gote">なし</div>
          <div class="cap-label" style="margin-top:0.9rem">先手持駒</div>
          <div class="cap-pieces" id="cap-sente">なし</div>
        </div>
      </div>
    </div>
    <div id="candidate-panel">
      <div id="cand-header-bar">— 局面を選択</div>
      <div id="cand-winrate-section">
        <div class="winrate-row">
          <span class="engine-label" style="color:var(--engine-mizusho)">水匠5</span>
          <div class="bar-container">
            <div id="bar-mizusho" class="win-bar" style="background:var(--engine-mizusho);width:50%"></div>
            <span class="bar-text" id="winrate-mizusho">—</span>
          </div>
        </div>
        <div class="winrate-row">
          <span class="engine-label" style="color:var(--engine-tanuki)">tanuki</span>
          <div class="bar-container">
            <div id="bar-tanuki" class="win-bar" style="background:var(--engine-tanuki);width:50%"></div>
            <span class="bar-text" id="winrate-tanuki">—</span>
          </div>
        </div>
        <div class="winrate-row">
          <span class="engine-label" style="color:#8adc9a">Kristall</span>
          <div class="bar-container">
            <div id="bar-kristallweizen" class="win-bar" style="background:#8adc9a;width:50%"></div>
            <span class="bar-text" id="winrate-kristallweizen">—</span>
          </div>
        </div>
        <div class="winrate-row">
          <span class="engine-label" style="color:#c88adc">dlshogi</span>
          <div class="bar-container">
            <div id="bar-dlshogi" class="win-bar" style="background:#c88adc;width:50%"></div>
            <span class="bar-text" id="winrate-dlshogi">—</span>
          </div>
        </div>
        <div id="divergence-line">乖離: —</div>
      </div>
      <div id="cand-grid">
        <div class="cand-col">
          <div class="cand-engine-label" style="color:var(--engine-mizusho)">水匠5</div>
          <ol id="candidates-mizusho" class="cand-list" style="list-style:none;padding:0;margin:0;"></ol>
        </div>
        <div class="cand-col">
          <div class="cand-engine-label" style="color:var(--engine-tanuki)">tanuki</div>
          <ol id="candidates-tanuki" class="cand-list" style="list-style:none;padding:0;margin:0;"></ol>
        </div>
        <div class="cand-col">
          <div class="cand-engine-label" style="color:#8adc9a">Kristallweizen</div>
          <ol id="candidates-kristallweizen" class="cand-list" style="list-style:none;padding:0;margin:0;"></ol>
        </div>
        <div class="cand-col">
          <div class="cand-engine-label" style="color:#c88adc">dlshogi</div>
          <ol id="candidates-dlshogi" class="cand-list" style="list-style:none;padding:0;margin:0;"></ol>
        </div>
      </div>
    </div>
    <div class="chart-container">
      <p class="chart-title">評価値推移 — Evaluation Graph</p>
      <div class="chart-legend">
        <div class="chart-legend-item"><div class="legend-dot" style="background:var(--engine-mizusho)"></div>水匠5</div>
        <div class="chart-legend-item"><div class="legend-dot" style="background:var(--engine-tanuki)"></div>tanuki</div>
        <div class="chart-legend-item"><div class="legend-dot" style="background:var(--accent-gold);opacity:0.6"></div>乖離</div>
      </div>
      <canvas id="evalChart" height="200"></canvas>
    </div>
    <div class="chart-container">
      <p class="chart-title">エンジン乖離 — Divergence (エンジン間最大乖離)</p>
      <canvas id="divergenceChart" height="100"></canvas>
    </div>
    <div class="chart-container">
      <p class="chart-title">重要局面 — Key Moments (乖離 Top 5)</p>
      <table class="moves-table">
        <thead><tr><th>#</th><th>手</th><th>水匠5</th><th>tanuki</th><th>乖離</th><th>注記</th></tr></thead>
        <tbody id="keyMoves"></tbody>
      </table>
    </div>
    <details style="margin-top:1px;">
      <summary style="font-family:var(--font-mono);font-size:0.7rem;letter-spacing:0.2em;color:var(--text-muted);padding:1rem 1.5rem;background:var(--bg-card);border:1px solid var(--border);cursor:pointer;list-style:none;">
        ▸ 全手評価値テーブル（クリックで展開）
      </summary>
      <div class="chart-container" style="margin-top:1px;">
        <table class="moves-table">
          <thead><tr><th>#</th><th>手</th><th>水匠5</th><th>tanuki</th><th>乖離</th><th>勝率</th></tr></thead>
          <tbody id="allMoves"></tbody>
        </table>
      </div>
    </details>
  </div>

  <div>
    <div class="sidebar-card">
      <p class="sidebar-card-title">対局情報</p>
      <div class="info-row"><span class="info-label">棋戦</span><span class="info-val">$kisen</span></div>
      <div class="info-row"><span class="info-label">日付</span><span class="info-val">$date</span></div>
      <div class="info-row"><span class="info-label">先手</span><span class="info-val">$sente</span></div>
      <div class="info-row"><span class="info-label">後手</span><span class="info-val">$gote</span></div>
      <div class="info-row"><span class="info-label">戦型</span><span class="info-val">$sentype</span></div>
      <div class="info-row"><span class="info-label">手数</span><span class="info-val">${total_moves}手</span></div>
    </div>
    <div class="sidebar-card" style="margin-top:1px;">
      <p class="sidebar-card-title">解析設定</p>
      <div class="info-row"><span class="info-label">エンジン1</span><span class="info-val" style="color:var(--engine-mizusho)">水匠5</span></div>
      <div class="info-row"><span class="info-label">エンジン2</span><span class="info-val" style="color:var(--engine-tanuki)">tanuki</span></div>
      <div class="info-row"><span class="info-label">エンジン3</span><span class="info-val" style="color:#8adc9a">Kristallweizen</span></div>
      <div class="info-row"><span class="info-label">エンジン4</span><span class="info-val" style="color:#c88adc">dlshogi</span></div>
      <div class="info-row"><span class="info-label">探索深さ</span><span class="info-val">depth $depth</span></div>
    </div>
    <div class="sidebar-card" style="margin-top:1px;">
      <p class="sidebar-card-title">乖離サマリー</p>
      <div class="info-row"><span class="info-label">最大乖離</span><span class="info-val" style="color:var(--accent-gold)">$div_max</span></div>
      <div class="info-row"><span class="info-label">発生局面</span><span class="info-val">${div_max_move}手目</span></div>
      <div class="info-row"><span class="info-label">平均乖離</span><span class="info-val">±$div_avg</span></div>
      <div class="info-row"><span class="info-label">乖離>100手</span><span class="info-val">${div_over100}手</span></div>
    </div>
  </div>
</div>

<footer>
  <span class="footer-logo">Kiri</span>
  <span class="footer-copy">© 2026 — 将棋 AI 解析プロジェクト</span>
</footer>

<script>
// =====================================================================
// 自作将棋盤エンジン — Kiri Board Engine (pure JS + SVG)
// =====================================================================

const KOMA_MAP = {
  '歩':1,'香':2,'桂':3,'銀':4,'金':5,'角':6,'飛':7,'王':8,'玉':8,
  'と':11,'成香':12,'成桂':13,'成銀':14,'馬':16,'竜':17,'龍':17
};
const KOMA_LABEL = ['','歩','香','桂','銀','金','角','飛','王','','','と','杏','圭','全','','馬','竜'];
const PROMOTED = {1:11,2:12,3:13,4:14,6:16,7:17};
const UNPROMOTE = {11:1,12:2,13:3,14:4,16:6,17:7};
const KANJI_NUM = {'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9};
const ZEN_NUM = {'１':1,'２':2,'３':3,'４':4,'５':5,'６':6,'７':7,'８':8,'９':9};

function initBoard() {
  const B = Array.from({length:9}, ()=>Array(9).fill(null));
  const r0g = [2,3,4,5,8,5,4,3,2];
  r0g.forEach((p,c)=>B[0][c]={p,s:-1});
  B[1][1]={p:7,s:-1}; B[1][7]={p:6,s:-1};
  for(let c=0;c<9;c++) B[2][c]={p:1,s:-1};
  for(let c=0;c<9;c++) B[6][c]={p:1,s:1};
  B[7][1]={p:6,s:1}; B[7][7]={p:7,s:1};
  const r8s = [2,3,4,5,8,5,4,3,2];
  r8s.forEach((p,c)=>B[8][c]={p,s:1});
  return B;
}

function parseKifMove(line, board) {
  const m = line.match(/^\s*\d+\s+(.+?)(?:\s*\(\d+:\d+.*\))?\s*$$/);
  if (!m) return null;
  let s = m[1].trim();
  s = s.replace(/\(\d+:\d+\/\d+:\d+:\d+\)\s*$$/, '').trim();
  s = s.replace(/\s*\(\d+:\d+\)\s*$$/, '').trim();
  if (s === '投了' || s === '中断' || s === '千日手' || s === '持将棋') return {end:true};

  const drop = s.match(/^([１-９])([一二三四五六七八九])([\S]+?)打$$/);
  if (drop) {
    const col = 9 - ZEN_NUM[drop[1]];
    const row = KANJI_NUM[drop[2]] - 1;
    const piece = drop[3];
    parseKifMove._lastTo = {r:row, c:col};
    return {to:{r:row,c:col}, drop:true, piece:KOMA_MAP[piece]||1};
  }

  const norm = s.match(/^(?:([１-９])([一二三四五六七八九])|同[　\s]*)(.+?)(成)?\((\d)(\d)\)$$/);
  if (norm) {
    let toR, toC;
    if (norm[1]) {
      toC = 9 - ZEN_NUM[norm[1]];
      toR = KANJI_NUM[norm[2]] - 1;
    } else {
      toR = parseKifMove._lastTo ? parseKifMove._lastTo.r : 0;
      toC = parseKifMove._lastTo ? parseKifMove._lastTo.c : 0;
    }
    const fromC = 9 - parseInt(norm[5]);
    const fromR = parseInt(norm[6]) - 1;
    const promote = norm[4] === '成';
    parseKifMove._lastTo = {r:toR, c:toC};
    return {from:{r:fromR,c:fromC}, to:{r:toR,c:toC}, promote};
  }
  return null;
}

function applyMove(board, caps, mv, sente) {
  const B = board.map(r=>r.map(c=>c?{...c}:null));
  const C = {s:{...caps.s}, g:{...caps.g}};
  if (mv.end) return {board:B, caps:C};

  const key = sente ? 's' : 'g';

  if (mv.drop) {
    const pid = mv.piece;
    const lbl = KOMA_LABEL[pid];
    if (C[key][lbl] > 0) C[key][lbl]--;
    B[mv.to.r][mv.to.c] = {p:pid, s:sente?1:-1};
  } else {
    const cell = B[mv.from.r][mv.from.c];
    if (!cell) return {board:B, caps:C};
    const target = B[mv.to.r][mv.to.c];
    if (target) {
      const base = UNPROMOTE[target.p] || target.p;
      const lbl = KOMA_LABEL[base];
      C[key][lbl] = (C[key][lbl]||0) + 1;
    }
    B[mv.from.r][mv.from.c] = null;
    const np = mv.promote ? (PROMOTED[cell.p]||cell.p) : cell.p;
    B[mv.to.r][mv.to.c] = {p:np, s:cell.s};
  }
  return {board:B, caps:C};
}

function buildPositions(kifLines) {
  let board = initBoard();
  let caps = {s:{}, g:{}};
  const positions = [{board:board.map(r=>r.map(c=>c?{...c}:null)), caps:{s:{...caps.s},g:{...caps.g}}, moveStr:'初期局面'}];
  parseKifMove._lastTo = null;

  for (const line of kifLines) {
    const tm = line.match(/^\s*(\d+)\s+/);
    if (!tm) continue;
    const num = parseInt(tm[1]);
    const mv = parseKifMove(line, board);
    if (!mv) continue;
    if (mv.end) break;
    const isSente = (num % 2 === 1);
    const res = applyMove(board, caps, mv, isSente);
    board = res.board;
    caps  = res.caps;
    const movePart = line.replace(/^\s*\d+\s+/, '').replace(/\s*\(\d+:\d+.*\)\s*$$/, '').trim();
    positions.push({
      board:board.map(r=>r.map(c=>c?{...c}:null)),
      caps:{s:{...caps.s},g:{...caps.g}},
      moveStr:`$${num}手目 — $${movePart}`
    });
  }
  return positions;
}

const kifLines = `$kif_text`.split('\n');

const positions = buildPositions(kifLines);
let boardIdx = 0;

function renderBoard(idx) {
  const pos = positions[Math.min(idx, positions.length-1)];
  const board = pos.board;
  const svg = document.getElementById('board-svg');
  const SZ = 396, MARGIN = 20, CELL = (SZ - MARGIN*2) / 9;
  svg.innerHTML = '';

  const ns = 'http://www.w3.org/2000/svg';
  const g = (tag, attrs, parent) => {
    const el = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));
    (parent||svg).appendChild(el); return el;
  };

  g('rect',{x:MARGIN,y:MARGIN,width:SZ-MARGIN*2,height:SZ-MARGIN*2,fill:'#2a2318',rx:'1'});

  for(let i=0;i<=9;i++){
    const x=MARGIN+i*CELL, y=MARGIN+i*CELL;
    g('line',{x1:x,y1:MARGIN,x2:x,y2:SZ-MARGIN,stroke:'rgba(255,255,255,0.12)','stroke-width':'0.5'});
    g('line',{x1:MARGIN,y1:y,x2:SZ-MARGIN,y2:y,stroke:'rgba(255,255,255,0.12)','stroke-width':'0.5'});
  }

  if (idx > 0) {
    const prevPos = positions[idx-1];
    for(let r=0;r<9;r++){
      for(let c=0;c<9;c++){
        const cur = board[r][c];
        const prv = prevPos.board[r][c];
        if (cur && (!prv || prv.p!==cur.p || prv.s!==cur.s)) {
          const cx = MARGIN + c*CELL, cy = MARGIN + r*CELL;
          g('rect',{x:cx,y:cy,width:CELL,height:CELL,fill:'rgba(200,169,110,0.18)'});
        }
      }
    }
  }

  for(let r=0;r<9;r++){
    for(let c=0;c<9;c++){
      const cell = board[r][c];
      if(!cell) continue;
      const cx = MARGIN + c*CELL + CELL/2;
      const cy = MARGIN + r*CELL + CELL/2;
      const isGote = cell.s === -1;
      const lbl = KOMA_LABEL[cell.p] || '?';
      const isPromoted = cell.p >= 11;
      const textCol = isPromoted ? '#c8a96e' : (isGote ? '#9ab8cc' : '#e8e4dc');

      const t = g('text',{
        x:cx, y:cy + CELL*0.3,
        'text-anchor':'middle',
        'dominant-baseline':'middle',
        fill: textCol,
        'font-size': CELL * 0.55,
        'font-family': 'Yu Mincho, YuMincho, 游明朝, Hiragino Mincho ProN, serif',
        transform: isGote ? `rotate(180,$${cx},$${cy})` : ''
      });
      t.textContent = lbl;
    }
  }

  const colLabels = ['９','８','７','６','５','４','３','２','１'];
  const rowLabels = ['一','二','三','四','五','六','七','八','九'];
  for(let i=0;i<9;i++){
    g('text',{x:MARGIN+i*CELL+CELL/2, y:MARGIN-4, 'text-anchor':'middle',
      'font-size':'7', fill:'rgba(255,255,255,0.3)', 'font-family':'var(--font-mono)'
    }).textContent = colLabels[i];
    g('text',{x:SZ-MARGIN+4, y:MARGIN+i*CELL+CELL/2, 'text-anchor':'start',
      'dominant-baseline':'middle', 'font-size':'7', fill:'rgba(255,255,255,0.3)',
      'font-family':'Yu Mincho, YuMincho, 游明朝, Hiragino Mincho ProN, serif'
    }).textContent = rowLabels[i];
  }

  document.getElementById('move-counter').textContent = pos.moveStr;

  const fmtCaps = (cap) => {
    const parts = Object.entries(cap).filter(([,n])=>n>0).map(([k,n])=>n>1?k+n:k);
    return parts.length ? parts.join(' ') : 'なし';
  };
  document.getElementById('cap-sente').textContent = fmtCaps(pos.caps.s);
  document.getElementById('cap-gote').textContent  = fmtCaps(pos.caps.g);
}

function updateCandidates(idx) {
  const headerEl = document.getElementById('cand-header-bar');
  let move = moves[idx];

  // idx が範囲外の場合の処理
  if (!move) {
    if (idx >= moves.length && moves.length > 0) {
      // 投了（最終局面）: 最後の手の評価値を使って表示する
      move = moves[moves.length - 1];
      headerEl.textContent = '投了 — 最終局面';
    } else {
      // idx < 0 または moves が空: 初期局面としてリセット
      headerEl.textContent = '初期局面';
      document.getElementById('bar-mizusho').style.width = '50%';
      document.getElementById('winrate-mizusho').textContent = '—';
      document.getElementById('bar-tanuki').style.width = '50%';
      document.getElementById('winrate-tanuki').textContent = '—';
      document.getElementById('bar-kristallweizen').style.width = '50%';
      document.getElementById('winrate-kristallweizen').textContent = '—';
      document.getElementById('bar-dlshogi').style.width = '50%';
      document.getElementById('winrate-dlshogi').textContent = '—';
      document.getElementById('divergence-line').textContent = '乖離: —';
      document.getElementById('candidates-mizusho').innerHTML = '';
      document.getElementById('candidates-tanuki').innerHTML = '';
      document.getElementById('candidates-kristallweizen').innerHTML = '';
      document.getElementById('candidates-dlshogi').innerHTML = '';
      return;
    }
  } else {
    headerEl.textContent = `$${move.num}手目局面 — 次の候補手`;
  }

  const scoreToWinrate = s => Math.round(100 / (1 + Math.exp(-s / 600)) * 10) / 10;
  const MATE_THRESHOLD = 29000;
  const isMate = (v) => v !== null && v !== undefined && Math.abs(v) >= MATE_THRESHOLD;
  const toDisplayScore = s => isMate(s) ? (s > 0 ? 3000 : -3000) : s;

  const s1 = move['水匠5'] ?? 0;
  const s2 = move['tanuki'] ?? 0;
  const wr1 = scoreToWinrate(toDisplayScore(s1));
  const wr2 = scoreToWinrate(toDisplayScore(s2));

  document.getElementById('bar-mizusho').style.width = wr1 + '%';
  document.getElementById('winrate-mizusho').textContent =
    `$${wr1}% ($${s1 >= 0 ? '+' : ''}$${s1}cp)`;
  document.getElementById('bar-tanuki').style.width = wr2 + '%';
  document.getElementById('winrate-tanuki').textContent =
    `$${wr2}% ($${s2 >= 0 ? '+' : ''}$${s2}cp)`;

  const s3 = move['Kristallweizen'] ?? 0;
  const s4 = move['dlshogi'] ?? 0;
  const wr3 = scoreToWinrate(toDisplayScore(s3));
  const wr4 = scoreToWinrate(toDisplayScore(s4));

  document.getElementById('bar-kristallweizen').style.width = wr3 + '%';
  document.getElementById('winrate-kristallweizen').textContent =
    `$${wr3}% ($${s3 >= 0 ? '+' : ''}$${s3}cp)`;
  document.getElementById('bar-dlshogi').style.width = wr4 + '%';
  document.getElementById('winrate-dlshogi').textContent =
    `$${wr4}% ($${s4 >= 0 ? '+' : ''}$${s4}cp)`;

  const scores = [s1, s2, s3, s4].filter(s => s !== null);
  const maxDiv = scores.length > 1 ? Math.max(...scores) - Math.min(...scores) : 0;
  document.getElementById('divergence-line').textContent =
    `エンジン間最大乖離: $${maxDiv}cp`;

  // 詰み検出表示
  const mateEngines = [
    ['水匠5', s1], ['tanuki', s2], ['Kristallweizen', s3], ['dlshogi', s4]
  ].filter(([, v]) => isMate(v));
  if (mateEngines.length > 0) {
    const winner = mateEngines[0][1] > 0 ? '先手勝ち' : '後手勝ち';
    headerEl.textContent += ` ── 詰み検出 ($${winner})`;
    headerEl.style.color = mateEngines[0][1] > 0 ? 'var(--engine-mizusho)' : 'var(--engine-tanuki)';
  } else {
    headerEl.style.color = '';
  }

  const renderList = (id, candidates) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = '';
    (candidates || []).forEach((c, i) => {
      const pvHtml = (c.pv_jp && c.pv_jp.length)
        ? c.pv_jp.map((p, j) =>
            `<span class="pv-move">$${j+1}.$${p}</span>`
          ).join('')
        : '';
      const li = document.createElement('li');
      li.className = 'cand-item';
      li.innerHTML = `
        <div class="cand-header">
          <span class="cand-rank">$${i+1}.</span>
          <span class="cand-move">$${c.move_jp}</span>
          <span class="cand-score">$${c.score >= 0 ? '+' : ''}$${c.score}</span>
          $${pvHtml ? '<span class="pv-toggle">▶</span>' : ''}
        </div>
        $${pvHtml ? `<div class="pv-line">$${pvHtml}</div>` : ''}
      `;
      if (pvHtml) {
        li.querySelector('.cand-header').addEventListener('click', () => {
          const pv = li.querySelector('.pv-line');
          const tog = li.querySelector('.pv-toggle');
          const isOpen = pv.classList.toggle('open');
          tog.textContent = isOpen ? '▼' : '▶';
          el.querySelectorAll('.pv-line.open').forEach(p => {
            if (p !== pv) {
              p.classList.remove('open');
              p.closest('.cand-item').querySelector('.pv-toggle').textContent = '▶';
            }
          });
        });
      }
      el.appendChild(li);
    });
  };

  renderList('candidates-mizusho', move.best_moves_mizusho);
  renderList('candidates-tanuki', move.best_moves_tanuki);
  renderList('candidates-kristallweizen', move.best_moves_kristallweizen);
  renderList('candidates-dlshogi', move.best_moves_dlshogi);
}

function boardGoto(idx) {
  boardIdx = Math.max(0, Math.min(idx, positions.length-1));
  renderBoard(boardIdx);
  highlightMove(boardIdx > 0 ? boardIdx - 1 : 0);
  updateCandidates(boardIdx);
}

function boardStep(d) {
  boardGoto(boardIdx + d);
}

renderBoard(0);

const moves = $moves_json;

const labels      = moves.map(m => m.num + "手");
// JSONは先手視点に統一済み（正=先手有利 / 負=後手有利）
const MATE_SCORE = 29000;
const clipScore = v => {
  if (v === null || v === undefined) return null;
  if (v >= MATE_SCORE) return 3000;
  if (v <= -MATE_SCORE) return -3000;
  return Math.max(-3000, Math.min(3000, v));
};
const mizushoData = moves.map(m => clipScore(m["水匠5"] ?? null));
const tanukiData  = moves.map(m => clipScore(m["tanuki"] ?? null));
const divData = moves.map(m => {
  const vals = ['水匠5','tanuki','Kristallweizen','dlshogi']
    .map(k => m[k])
    .filter(v => v != null && Math.abs(v) < MATE_SCORE);
  if (vals.length < 2) return null;
  const d = Math.max(...vals) - Math.min(...vals);
  return Math.min(d, 3000);
});

const chartCfg = {
  responsive: true,
  plugins: { legend: { display: false }, tooltip: {
    backgroundColor: "#1a1918", borderColor: "rgba(255,255,255,0.1)", borderWidth: 1,
    titleColor: "#9a9490", bodyColor: "#e8e4dc",
    titleFont: { family: "JetBrains Mono", size: 11 },
    bodyFont:  { family: "JetBrains Mono", size: 12 },
  }},
  scales: {
    x: { ticks: { color:"#5a5652", font:{family:"JetBrains Mono",size:10} }, grid:{color:"rgba(255,255,255,0.04)"} },
    y: { min:-3000, max:3000, ticks: { color:"#5a5652", font:{family:"JetBrains Mono",size:10} }, grid:{color:"rgba(255,255,255,0.04)"} },
  }
};

function onChartClick(e, elements) {
  if (elements.length > 0) {
    const idx = elements[0].index;
    boardGoto(idx + 1);
  }
}

function highlightMove(idx) {
  const tbody = document.getElementById("allMoves");
  if (!tbody) return;
  const rows = tbody.querySelectorAll("tr");
  rows.forEach(r => r.classList.remove("highlight-move"));
  if (rows[idx]) {
    rows[idx].classList.add("highlight-move");
    rows[idx].scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

new Chart(document.getElementById("evalChart"), {
  type: "line",
  data: { labels, datasets: [
    { label:"水匠5", data:mizushoData, borderColor:"#6aacdc", borderWidth:1.5, pointRadius:0, tension:0.3, spanGaps:true },
    { label:"tanuki", data:tanukiData, borderColor:"#dc8a6a", borderWidth:1.5, pointRadius:0, tension:0.3, spanGaps:true },
  ]},
  options: { ...chartCfg, onClick: onChartClick },
});

new Chart(document.getElementById("divergenceChart"), {
  type: "bar",
  data: { labels, datasets: [{
    label:"乖離", data:divData,
    backgroundColor: divData.map(v => v != null && Math.abs(v) > 80 ? "rgba(200,169,110,0.6)" : "rgba(200,169,110,0.2)"),
    borderColor: "rgba(200,169,110,0.4)", borderWidth:1,
  }]},
  options: { ...chartCfg, onClick: onChartClick },
});

// 乖離Top5
const sorted = [...moves].filter(m => m["水匠5"] != null && m["tanuki"] != null)
  .sort((a,b) => Math.abs(b["tanuki"]-b["水匠5"]) - Math.abs(a["tanuki"]-a["水匠5"])).slice(0,5);
const keyTbody = document.getElementById("keyMoves");
sorted.forEach(m => {
  const d = m["tanuki"] - m["水匠5"];
  keyTbody.innerHTML += `<tr>
    <td class="move-num">$${m.num}</td>
    <td class="move-jp">$${m.move_jp}</td>
    <td class="val-pos">$${m["水匠5"] >= 0 ? "+" : ""}$${m["水匠5"]}</td>
    <td class="val-pos">$${m["tanuki"] >= 0 ? "+" : ""}$${m["tanuki"]}</td>
    <td style="color:var(--accent-gold);font-weight:500">$${d >= 0 ? "+" : ""}$${d}</td>
    <td style="color:var(--text-muted);font-size:0.7rem">$${Math.abs(d) > 100 ? "霧の核心" : Math.abs(d) > 50 ? "要注目" : ""}</td>
  </tr>`;
});

// 全手テーブル
const allTbody = document.getElementById("allMoves");
moves.forEach(m => {
  const d = (m["水匠5"] != null && m["tanuki"] != null) ? m["tanuki"] - m["水匠5"] : null;
  allTbody.innerHTML += `<tr>
    <td class="move-num">$${m.num}</td>
    <td class="move-jp">$${m.move_jp}</td>
    <td class="val-pos">$${m["水匠5"] != null ? (m["水匠5"] >= 0 ? "+" : "") + m["水匠5"] : "—"}</td>
    <td class="val-pos">$${m["tanuki"] != null ? (m["tanuki"] >= 0 ? "+" : "") + m["tanuki"] : "—"}</td>
    <td style="color:$${d != null && Math.abs(d) > 80 ? "var(--accent-gold)" : "var(--text-secondary)"}">$${d != null ? (d >= 0 ? "+" : "") + d : "—"}</td>
    <td style="color:var(--text-muted)">$${m.winrate}%</td>
  </tr>`;
});
</script>
</body>
</html>
''')


def generate_html(meta: dict, results: list, kif_path: Path, depth: int) -> str:
    """解析結果からHTMLを生成"""
    sente   = meta.get("先手", "先手")
    gote    = meta.get("後手", "後手")
    kisen   = meta.get("棋戦", "")
    sentype = meta.get("戦型", "")
    date_raw = meta.get("開始日時", "")
    date    = date_raw.split(" ")[0].replace("/", ".") if date_raw else ""
    year    = date.split(".")[0] if date else ""

    # 乖離サマリー
    divs = [abs(r.get("tanuki", 0) - r.get("水匠5", 0))
            for r in results if r.get("水匠5") is not None and r.get("tanuki") is not None]
    div_vals = [(r.get("tanuki", 0) - r.get("水匠5", 0), r["num"])
                for r in results if r.get("水匠5") is not None and r.get("tanuki") is not None]
    div_max_val, div_max_move = max(div_vals, key=lambda x: abs(x[0]), default=(0, 0))
    div_avg = int(sum(divs) / len(divs)) if divs else 0
    div_over100 = sum(1 for d in divs if d > 100)

    # KIFファイルの内容を読み込んで盤面再生用に埋め込む
    kif_text = kif_path.read_text(encoding="utf-8", errors="replace").rstrip()

    # div_max を符号付き文字列にフォーマット
    div_max_str = f"{div_max_val:+d}"

    return GAME_HTML_TEMPLATE.safe_substitute(
        sente=sente, gote=gote, kisen=kisen, sentype=sentype,
        date=date, year=year, total_moves=len(results), depth=depth,
        div_max=div_max_str, div_max_move=div_max_move,
        div_avg=div_avg, div_over100=div_over100,
        kif_text=kif_text,
        moves_json=json.dumps(results, ensure_ascii=False, indent=2),
    )


# ============================================================
# index.html の games-grid 自動更新
# ============================================================
def update_index_html(docs_dir: Path):
    """docs/games/*.json を読み込んで docs/index.html の games-grid を更新する"""
    import math

    games_dir = docs_dir / "games"
    index_path = docs_dir / "index.html"
    if not index_path.exists():
        print(f"  ⚠ index.html が見つかりません: {index_path}")
        return

    # 全JSONを読み込む
    json_files = sorted(games_dir.glob("*.json"))
    games = []
    for jf in json_files:
        if jf.name.endswith(".json.bak"):
            continue
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠ JSON読み込み失敗: {jf} ({e})")
            continue
        meta = data.get("meta", {})
        results = data.get("moves", [])

        # 乖離サマリー
        div_vals = [(r.get("tanuki", 0) - r.get("水匠5", 0), r["num"])
                    for r in results if r.get("水匠5") is not None and r.get("tanuki") is not None]
        div_max_val, div_max_move = max(div_vals, key=lambda x: abs(x[0]), default=(0, 0))

        # 最終手の評価値（カード用）
        last = results[-1] if results else {}
        s1 = last.get("水匠5", 0) or 0
        s2 = last.get("tanuki", 0) or 0

        def score_to_bar(s):
            """評価値からバー幅%を算出"""
            wr = 100 / (1 + math.exp(-s / 600))
            return max(5, min(95, round(wr)))

        # 手数（投了除く）
        total_moves = len(results)

        # 先手/後手勝ちを判定
        if s1 > 0:
            winner = "先手勝"
        elif s1 < 0:
            winner = "後手勝"
        else:
            winner = ""

        date_raw = meta.get("開始日時", "")
        date_str = date_raw.split(" ")[0].replace("/", ".") if date_raw else ""

        games.append({
            "stem": jf.stem,
            "sente": meta.get("先手", "?"),
            "gote": meta.get("後手", "?"),
            "kisen": meta.get("棋戦", ""),
            "date": date_str,
            "date_raw": date_raw,
            "sentype": meta.get("戦型", ""),
            "total_moves": total_moves,
            "winner": winner,
            "s1": s1, "s2": s2,
            "bar1": score_to_bar(s1),
            "bar2": score_to_bar(s2),
            "div_max_val": div_max_val,
            "div_max_move": div_max_move,
        })

    if not games:
        print("  ⚠ ゲームデータがありません")
        return

    # 最新ゲームを判定（開始日時が最新のもの）
    games.sort(key=lambda g: g["date_raw"], reverse=True)
    newest_stem = games[0]["stem"]

    # カードHTML生成
    cards_html = []
    for g in games:
        badge = "NEW" if g["stem"] == newest_stem else "名局アーカイブ"
        s1_str = f"{g['s1']:+d}" if g['s1'] != 0 else "0"
        s2_str = f"{g['s2']:+d}" if g['s2'] != 0 else "0"
        div_str = f"{g['div_max_val']:+d}"
        card = f'''    <a href="games/{g['stem']}.html" class="game-card">
      <div class="game-card-thumb">
        <span class="game-card-badge">{badge}</span>
      </div>
      <div class="game-card-body">
        <p class="game-card-meta">{g['date']} — {g['kisen']}</p>
        <p class="game-card-title">{g['sente']} vs {g['gote']}</p>
        <p class="game-card-sub">{g['sentype']} ／ {g['total_moves']}手 {g['winner']}</p>
        <div class="engine-bars">
          <div class="engine-bar-row">
            <span class="engine-name">水匠5</span>
            <div class="engine-bar-track">
              <div class="engine-bar-fill mizusho" style="width:{g['bar1']}%"></div>
            </div>
            <span class="engine-val">{s1_str}</span>
          </div>
          <div class="engine-bar-row">
            <span class="engine-name">tanuki</span>
            <div class="engine-bar-track">
              <div class="engine-bar-fill tanuki" style="width:{g['bar2']}%"></div>
            </div>
            <span class="engine-val">{s2_str}</span>
          </div>
        </div>
        <div class="divergence-badge">△ 乖離 max {div_str} ({g['div_max_move']}手目)</div>
      </div>
    </a>'''
        cards_html.append(card)

    new_grid = "<!-- GAMES-GRID-START -->\n" + "\n\n".join(cards_html) + "\n    <!-- GAMES-GRID-END -->"

    html = index_path.read_text(encoding="utf-8")

    # まずコメントタグ方式で置換を試みる
    marker_pattern = r'<!-- GAMES-GRID-START -->.*?<!-- GAMES-GRID-END -->'
    new_html, count = re.subn(marker_pattern, new_grid, html, count=1, flags=re.DOTALL)

    if not count:
        # コメントタグがない場合: <div class="games-grid"> ～ 対応する </div> を正規表現で探す
        grid_pattern = r'(<div class="games-grid">)\s*.*?\n  </div>'
        replacement = f'\\1\n{new_grid}\n  </div>'
        new_html, count = re.subn(grid_pattern, replacement, html, count=1, flags=re.DOTALL)

    if count:
        index_path.write_text(new_html, encoding="utf-8")
        print(f"  ✓ index.html games-grid 更新: {index_path}")
    else:
        print(f"  ⚠ index.html games-grid 置換失敗")


# ============================================================
# 既存HTMLのインライン moves 更新
# ============================================================
def _update_html_moves(html_path: Path, results: list):
    """既存HTMLファイル内の const moves = [...]; を新しいデータで置換"""
    html = html_path.read_text(encoding="utf-8")
    # "const moves = [" で始まり "];" で終わるブロックを検出・置換
    pattern = r'(const moves = )\[.*?\];'
    new_data = json.dumps(results, ensure_ascii=False, indent=2)
    replacement = f'\\1{new_data};'
    new_html, count = re.subn(pattern, replacement, html, count=1, flags=re.DOTALL)
    if count:
        html_path.write_text(new_html, encoding="utf-8")
        print(f"  ✓ HTMLインライン moves 更新: {html_path}")
    else:
        print(f"  ⚠ HTMLインライン moves 置換失敗: {html_path}")


# ============================================================
# メイン
# ============================================================
kif_path_global = None  # analyze_all_moves内から参照するグローバル

def process_kif(kif_path: Path, depth: int = 12, json_only: bool = False):
    print(f"\n{'='*50}")
    print(f"  処理中: {kif_path.name}")
    print(f"{'='*50}")

    global kif_path_global
    kif_path_global = kif_path

    # パース
    parsed = parse_kif(kif_path)
    meta   = parsed["meta"]
    moves  = parsed["moves"]
    print(f"  {meta.get('先手','?')} vs {meta.get('後手','?')} — {len(moves)}手")

    # 解析
    print(f"  4エンジン解析開始 (depth={depth})...")
    if not ENGINE_PATH.exists():
        print(f"  ⚠ エンジンが見つかりません: {ENGINE_PATH}")
        print(f"    ダミーデータで続行します")
        results = _dummy_analysis(moves)
    else:
        results = analyze_all_moves(moves, ENGINE_PATH, depth=depth)

    # JSON保存
    stem = kif_path.stem
    json_path = GAMES_DIR / f"{stem}.json"
    GAMES_DIR.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "moves": results}, f, ensure_ascii=False, indent=2)
    print(f"  ✓ JSON保存: {json_path}")

    # 既存HTMLのインライン moves データを更新
    html_path = GAMES_DIR / f"{stem}.html"
    if html_path.exists():
        _update_html_moves(html_path, results)

    if json_only:
        return

    # HTML生成
    html = generate_html(meta, results, kif_path, depth)
    html_path = GAMES_DIR / f"{stem}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ HTML生成: {html_path}")
    print(f"  → ブラウザで開く: file://{html_path}")

    # index.html の games-grid を更新
    update_index_html(DOCS_DIR)


def process_html_only(kif_path: Path, depth: int = 12):
    """既存JSONからHTMLのみを再生成する"""
    stem = kif_path.stem
    json_path = GAMES_DIR / f"{stem}.json"

    if not json_path.exists():
        print(f"  ⚠ JSONファイルが見つかりません: {json_path}")
        return

    print(f"\n{'='*50}")
    print(f"  HTML再生成: {kif_path.name}")
    print(f"{'='*50}")

    # JSON読み込み
    data = json.loads(json_path.read_text(encoding="utf-8"))
    meta = data["meta"]
    results = data["moves"]
    print(f"  {meta.get('先手','?')} vs {meta.get('後手','?')} — {len(results)}手")

    # HTML生成
    html = generate_html(meta, results, kif_path, depth)
    html_path = GAMES_DIR / f"{stem}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  ✓ HTML生成: {html_path}")

    # index.html の games-grid を更新
    update_index_html(DOCS_DIR)


def main():
    parser = argparse.ArgumentParser(description="Kiri — KIF → 解析 → HTML生成")
    parser.add_argument("kif_files", nargs="+", help="KIFファイルパス")
    parser.add_argument("--depth", type=int, default=12, help="探索深さ (default: 12)")
    parser.add_argument("--json-only", action="store_true", help="JSON生成のみ（HTML生成しない）")
    parser.add_argument("--html-only", action="store_true", help="既存JSONからHTMLのみ再生成")
    args = parser.parse_args()

    for kif_pattern in args.kif_files:
        from glob import glob
        paths = glob(kif_pattern)
        if not paths:
            print(f"  ⚠ ファイルが見つかりません: {kif_pattern}")
            continue
        for p in paths:
            if args.html_only:
                process_html_only(Path(p), depth=args.depth)
            else:
                process_kif(Path(p), depth=args.depth, json_only=args.json_only)

    print("\n完了。")


if __name__ == "__main__":
    main()
