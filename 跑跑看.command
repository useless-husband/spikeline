#!/bin/bash
# ============================================================
#  跑跑看：在 Mac 上模擬「腦機介面訊號晶片」並看結果圖
#
#  這個檔案在 Finder 裡雙擊就會打開「終端機」來執行。
#  它會做三件事：
#    1. 檢查需要的工具（verilator、python3、c++、make）有沒有裝。
#    2. 第一次執行時建立 Python 虛擬環境 .venv，安裝 numpy 等套件
#       （需要網路，大約 1 分鐘；之後就不用再裝）。
#    3. 用 Verilator 把 rtl/ 資料夾裡的電路（SystemVerilog）編譯成模擬程式，
#       餵進 0.4 秒、96 個電極的合成腦波，
#       把晶片「吐出來」的資料畫成一張圖並自動打開。
#
#  圖上除了最左邊的原始訊號，其他每一條線都來自模擬晶片的輸出，
#  不是另外用軟體算的。
# ============================================================

# 先切換到這個檔案所在的資料夾。資料夾名稱有空白和中文，
# 所以 "$(dirname "$0")" 一定要用雙引號包起來。
cd "$(dirname "$0")" || exit 1

fail() {
  echo ""
  echo "$1"
  read -r -p "按 Enter 關閉視窗..."
  exit 1
}

# 1. 檢查工具
for tool in verilator python3 make c++; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    case "$tool" in
      verilator) fail "找不到 verilator。請先在終端機執行：brew install verilator" ;;
      python3)   fail "找不到 python3。請到 https://www.python.org 下載安裝 Python 3。" ;;
      *)         fail "找不到 $tool。請在終端機執行：xcode-select --install（安裝 Xcode 命令列工具）" ;;
    esac
  fi
done
echo "工具都在：$(verilator --version | head -1)"

# 2. Python 套件（只有第一次會裝）
if [ ! -x .venv/bin/python ]; then
  echo "第一次執行：建立 .venv 並安裝 numpy、scipy、matplotlib、cocotb..."
  make venv || fail "安裝 Python 套件失敗（需要網路）。上面的訊息會說明原因。"
fi

# 3. 編譯模擬程式並產生圖片
echo "編譯晶片模擬程式（第一次大約 30 秒，之後很快）並執行..."
make demo || fail "模擬失敗，上面的訊息會說明原因。"

echo ""
echo "完成！圖片在 build/demo/demo.png，現在幫你打開。"
open build/demo/demo.png
read -r -p "按 Enter 關閉視窗..."
