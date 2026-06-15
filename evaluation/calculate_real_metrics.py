#!/usr/bin/env python3
"""
CALCULATE REAL PRECISION/RECALL FROM HUMAN ANNOTATIONS
This gives you the TRUTH about your system's accuracy
"""
import json
from pathlib import Path

def load_annotations(annotation_file):
    """Load human annotations"""
    with open(annotation_file, 'r') as f:
        return json.load(f)

def calculate_metrics_from_annotations(annotations):
    """Calculate precision from annotations"""
    tp = sum(1 for a in annotations if a.get('correct') == True)
    fp = sum(1 for a in annotations if a.get('correct') == False)
    uncertain = sum(1 for a in annotations if a.get('correct') is None)
    total = len(annotations)
    
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    
    return {
        'precision': precision,
        'tp': tp,
        'fp': fp,
        'uncertain': uncertain,
        'total': total
    }

def main():
    print("="*60)
    print("REAL METRICS FROM HUMAN ANNOTATION")
    print("="*60)
    print("\nThis calculates your ACTUAL precision based on human verification.\n")
    
    all_results = {}
    
    # Find all annotation files
    annotation_files = list(Path('.').glob('annotations_*.json'))
    
    if not annotation_files:
        print("❌ No annotation files found!")
        print("\nYou need to:")
        print("1. Run: python3 ground_truth_annotator.py")
        print("2. Open the HTML files in your browser")
        print("3. Annotate the calls (click ✅ or ❌)")
        print("4. Click 'Save & Export'")
        print("5. Run this script again")
        return
    
    for ann_file in annotation_files:
        data = load_annotations(ann_file)
        repo_name = data.get('repository', ann_file.stem.replace('annotations_', ''))
        metrics = calculate_metrics_from_annotations(data['annotations'])
        all_results[repo_name] = metrics
        
        print(f"\n📁 {repo_name.upper()}")
        print(f"   Annotated: {metrics['total']} calls")
        print(f"   ✅ Correct (TP): {metrics['tp']}")
        print(f"   ❌ Incorrect (FP): {metrics['fp']}")
        print(f"   ❓ Uncertain: {metrics['uncertain']}")
        print(f"   📊 Precision: {metrics['precision']:.2%}")
        
        # Grade
        if metrics['precision'] >= 0.9:
            print(f"   🏆 Grade: A+ - Excellent!")
        elif metrics['precision'] >= 0.8:
            print(f"   👍 Grade: A - Very Good!")
        elif metrics['precision'] >= 0.7:
            print(f"   📈 Grade: B - Good, needs some improvement")
        elif metrics['precision'] >= 0.6:
            print(f"   ⚠️ Grade: C - Acceptable, but many false positives")
        else:
            print(f"   ❌ Grade: F - Poor, high false positive rate")
    
    # Summary
    if all_results:
        avg_precision = sum(r['precision'] for r in all_results.values()) / len(all_results)
        total_tp = sum(r['tp'] for r in all_results.values())
        total_fp = sum(r['fp'] for r in all_results.values())
        total_annotated = sum(r['total'] for r in all_results.values())
        
        print("\n" + "="*60)
        print("OVERALL REAL PRECISION")
        print("="*60)
        print(f"Total annotated calls: {total_annotated}")
        print(f"Total correct (TP): {total_tp}")
        print(f"Total incorrect (FP): {total_fp}")
        print(f"Average Precision: {avg_precision:.2%}")
        
        # Final verdict
        print("\n" + "="*60)
        print("FINAL VERDICT")
        print("="*60)
        if avg_precision >= 0.85:
            print("🎉 EXCELLENT! Your system is highly accurate.")
            print("   The 406k+ calls detected are likely mostly correct.")
            print("   Ready for SWE benchmark submission!")
        elif avg_precision >= 0.70:
            print("👍 GOOD - Your system works but has false positives.")
            print("   Investigate common error patterns to improve.")
        else:
            print("⚠️ NEEDS IMPROVEMENT - Too many false positives.")
            print("   The raw call count may be misleading.")
        
        # Save results
        with open('real_precision_results.json', 'w') as f:
            json.dump(all_results, f, indent=2)
        print("\n💾 Results saved to real_precision_results.json")

if __name__ == "__main__":
    main()
