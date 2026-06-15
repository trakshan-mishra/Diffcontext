import json
import sys
from dependency_graph import DependencyGraph
from extractor import CodeExtractor

def evaluate(repo_path, expected_path):
    extractor = CodeExtractor(repo_path)
    codebase = extractor.extract()
    graph = DependencyGraph(codebase)
    graph.build()
    
    predicted = set(graph.get_all_edges())
    
    with open(expected_path) as f:
        gt_dict = json.load(f)
    
    ground_truth = set()
    for caller, callees in gt_dict.items():
        for callee in callees:
            ground_truth.add((caller, callee))
    
    tp = len(predicted & ground_truth)
    fp = len(predicted - ground_truth)
    fn = len(ground_truth - predicted)
    
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    f1 = 2 * p * r / (p + r) if p + r else 0
    
    print(f"Precision: {p:.3f}")
    print(f"Recall: {r:.3f}")
    print(f"F1: {f1:.3f}")
    print(f"TP: {tp}, FP: {fp}, FN: {fn}")

if __name__ == "__main__":
    evaluate(sys.argv[1], sys.argv[2])
