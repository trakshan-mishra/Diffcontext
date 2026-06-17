#!/bin/bash

echo "🏆 RUNNING FINAL VALIDATION SUITE"
echo "=================================="
echo ""

# Run all analyzers and collect metrics
python3 genuine_validation.py > final_report.txt 2>&1

# Extract key metrics
echo "📊 KEY METRICS SUMMARY" >> final_report.txt
echo "=====================" >> final_report.txt
grep "Functions:" final_report.txt | head -1 >> final_report.txt
grep "Calls:" final_report.txt | head -1 >> final_report.txt
grep "Decorators:" final_report.txt | head -1 >> final_report.txt
grep "Inheritance:" final_report.txt | head -1 >> final_report.txt

echo "" >> final_report.txt
echo "📈 DETAILED BY REPOSITORY" >> final_report.txt
echo "========================" >> final_report.txt
grep -A 8 "Analyzing:" final_report.txt | grep -E "Analyzing:|Functions:|Calls:|Decorators:|Inheritance:" >> final_report.txt

echo ""
echo "✅ Final report saved to final_report.txt"
echo ""
echo "📋 WHAT TO PRESENT TO CLAUDE/SWE EXPERTS:"
echo ""
echo "1. Show final_report.txt - raw numbers"
echo "2. Show verification_package/ - independent validation tools"
echo "3. Show genuine_capability.json - complete metrics"
echo "4. Explain methodology - AST parsing, no hardcoding"
echo "5. Acknowledge limitations - dynamic imports, Cython"
echo ""
echo "🎯 Your system is ready for human evaluation!"
