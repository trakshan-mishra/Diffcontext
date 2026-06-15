#!/bin/bash
echo ""
echo "📦 INSTALLING pyCG-bench (The RIGHT tool for your use case)"
echo "==========================================================="

# Create fresh environment
cd ~/temporary
python3 -m venv pycg_bench_env
source pycg_bench_env/bin/activate

# Install pyCG-bench
git clone https://github.com/vrth/pyCG-bench.git
cd pyCG-bench
pip install -e .

# Install your analyzer (adjust path!)
# pip install -e /path/to/your/analyzer

echo "✅ pyCG-bench installed"
echo ""
echo "pyCG-bench tests EXACTLY what you need:"
echo "  → Call graph construction precision"
echo "  → Recall across different Python features"
echo "  → Handling of dynamic code, decorators, etc."
echo "  → Comparison against 5 other analyzers"
