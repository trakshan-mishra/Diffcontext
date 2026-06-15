#!/usr/bin/env python3
"""
GROUND TRUTH ANNOTATION TOOL
Creates random samples for human verification
Generates precision/recall from your annotations
"""
import json
import random
import csv
from pathlib import Path
from datetime import datetime

class GroundTruthAnnotator:
    def __init__(self):
        self.annotations = []
        
    def create_verification_samples(self, repo_path: str, repo_name: str, sample_size: int = 50):
        """Create random samples for human verification"""
        # Import here to avoid circular imports
        import sys
        sys.path.insert(0, '.')
        from enhanced_dependency_graph import build_enhanced_graph
        
        print(f"\n📊 Creating samples for {repo_name}...")
        
        # Check if repo exists
        if not Path(repo_path).exists():
            print(f"  ⚠️ Repo not found: {repo_path}")
            return None, None
        
        graph = build_enhanced_graph(repo_path)
        
        # Get all function calls with their locations
        all_calls = []
        for caller, callee in graph['calls']:
            all_calls.append({
                'caller': caller,
                'callee': callee,
                'file': caller.split('::')[0] if '::' in caller else caller,
                'verified': None,
                'correct': None,
                'notes': ''
            })
        
        if not all_calls:
            print(f"  ⚠️ No calls found in {repo_name}")
            return None, None
        
        # Random sample
        if len(all_calls) > sample_size:
            samples = random.sample(all_calls, sample_size)
        else:
            samples = all_calls
        
        # Save to CSV for easy annotation
        csv_file = f"ground_truth_{repo_name}.csv"
        with open(csv_file, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['caller', 'callee', 'file', 'verified', 'correct', 'notes'])
            writer.writeheader()
            writer.writerows(samples)
        
        print(f"  ✅ Created {csv_file} with {len(samples)} calls to verify")
        
        # Create HTML interface for easier annotation
        html_file = f"annotate_{repo_name}.html"
        self._create_annotation_interface(samples, repo_name, html_file)
        
        return csv_file, html_file
    
    def _create_annotation_interface(self, samples, repo_name, html_file):
        """Create web interface for annotation"""
        html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Annotate {repo_name} - Ground Truth</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; }}
        .call {{ border: 1px solid #ccc; margin: 10px; padding: 10px; border-radius: 5px; }}
        .correct {{ background-color: #d4edda; }}
        .incorrect {{ background-color: #f8d7da; }}
        button {{ margin: 5px; padding: 5px 10px; cursor: pointer; }}
        .nav {{ position: fixed; top: 0; right: 20px; background: white; padding: 10px; border: 1px solid #ccc; border-radius: 5px; }}
        code {{ background: #f4f4f4; padding: 2px 5px; border-radius: 3px; }}
        textarea {{ width: 100%; margin-top: 10px; }}
        .progress {{ font-weight: bold; }}
    </style>
</head>
<body>
    <div class="nav">
        <strong>Progress:</strong> <span id="progress">0</span>/<span id="total">{len(samples)}</span>
        <button onclick="saveAndExport()">💾 Save & Export</button>
    </div>
    <h1>📝 Annotate {repo_name} - Function Call Correctness</h1>
    <p>For each function call, verify if the callee function actually exists in the codebase.</p>
    <p><strong>Instructions:</strong></p>
    <ul>
        <li>Open the file path shown in your editor</li>
        <li>Check if the function being called exists</li>
        <li>Click ✅ if it exists, ❌ if it doesn't</li>
        <li>Add notes if needed</li>
    </ul>
    <div id="calls"></div>
    
    <script>
        const samples = {json.dumps(samples, indent=2)};
        let currentIndex = 0;
        let annotations = {{}};
        
        function renderCall(index) {{
            const call = samples[index];
            if (!call) return;
            
            const container = document.getElementById('calls');
            container.innerHTML = `
                <div class="call" id="call-card">
                    <h3>Call #${{index + 1}} of {len(samples)}</h3>
                    <p><strong>📁 Caller:</strong><br><code>${{call.caller}}</code></p>
                    <p><strong>🎯 Callee:</strong><br><code>${{call.callee}}</code></p>
                    <p><strong>📄 File to check:</strong><br><code>${{call.file}}</code></p>
                    <p><strong>🔍 How to verify:</strong><br>
                       Run: <code>grep -r "def ${{call.callee.split('.')[-1]}}" $(echo ${{call.file}} | cut -d'/' -f1-2)</code>
                    </p>
                    <p>
                        <button style="background:#28a745;color:white" onclick="verify(true)">✅ CORRECT - Callee exists</button>
                        <button style="background:#dc3545;color:white" onclick="verify(false)">❌ INCORRECT - Callee doesn't exist</button>
                        <button onclick="verify(null)">❓ SKIP / CAN'T DETERMINE</button>
                    </p>
                    <p>
                        <textarea id="notes" placeholder="Add notes (optional)" rows="2" cols="80"></textarea>
                    </p>
                    <div class="progress">Progress: ${{Object.keys(annotations).length}}/{len(samples)} annotated</div>
                </div>
            `;
            
            // Load existing annotation if any
            const key = call.caller + '|' + call.callee;
            if (annotations[key]) {{
                const noteField = document.getElementById('notes');
                if (noteField) noteField.value = annotations[key].notes || '';
            }}
            
            document.getElementById('progress').innerText = Object.keys(annotations).length;
        }}
        
        function verify(correct) {{
            const call = samples[currentIndex];
            const notes = document.getElementById('notes').value;
            const key = call.caller + '|' + call.callee;
            
            annotations[key] = {{
                caller: call.caller,
                callee: call.callee,
                file: call.file,
                correct: correct,
                notes: notes,
                timestamp: new Date().toISOString()
            }};
            
            currentIndex++;
            if (currentIndex < samples.length) {{
                renderCall(currentIndex);
            }} else {{
                document.getElementById('calls').innerHTML = '<h2>✅ All calls annotated!</h2><p>Click "Save & Export" to download your annotations.</p>';
            }}
            document.getElementById('progress').innerText = Object.keys(annotations).length;
        }}
        
        function saveAndExport() {{
            const data = {{
                repository: '{repo_name}',
                annotations: Object.values(annotations),
                total_annotated: Object.keys(annotations).length,
                timestamp: new Date().toISOString()
            }};
            
            // Download as JSON
            const blob = new Blob([JSON.stringify(data, null, 2)], {{type: 'application/json'}});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'annotations_{repo_name}.json';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            
            alert('✅ Annotations saved! Check your downloads folder.');
        }}
        
        renderCall(0);
    </script>
</body>
</html>
"""
        with open(html_file, 'w') as f:
            f.write(html_content)
        print(f"  ✅ Created {html_file} - open in browser to annotate")
    
    def calculate_precision_recall(self, annotation_file: str):
        """Calculate real precision/recall from human annotations"""
        with open(annotation_file, 'r') as f:
            data = json.load(f)
        
        annotations = data['annotations']
        
        tp = sum(1 for a in annotations if a.get('correct') == True)
        fp = sum(1 for a in annotations if a.get('correct') == False)
        uncertain = sum(1 for a in annotations if a.get('correct') is None)
        total_annotated = len(annotations)
        
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0
        
        print(f"\n{'='*50}")
        print(f"PRECISION RESULTS for {data['repository']}")
        print(f"{'='*50}")
        print(f"Total annotated: {total_annotated}")
        print(f"True Positives (correct): {tp}")
        print(f"False Positives (incorrect): {fp}")
        print(f"Uncertain: {uncertain}")
        print(f"Precision: {precision:.2%}")
        
        if precision >= 0.9:
            print(f"   ✅ Excellent! Your calls are mostly correct.")
        elif precision >= 0.75:
            print(f"   👍 Good - Some false positives to investigate.")
        elif precision >= 0.6:
            print(f"   ⚠️ Acceptable - But many false positives.")
        else:
            print(f"   ❌ Poor - Most detected calls may be wrong.")
        
        print(f"\nNote: Recall requires full ground truth (annotating ALL calls)")
        
        return {'precision': precision, 'tp': tp, 'fp': fp, 'total': total_annotated}

def main():
    annotator = GroundTruthAnnotator()
    
    print("="*60)
    print("GROUND TRUTH ANNOTATION TOOL")
    print("="*60)
    print("\nThis tool creates HTML files for you to manually verify function calls.")
    print("You need to actually open the files and check if calls are correct.\n")
    
    # List of repositories to annotate
    repos = [
        ('real_benchmarks/fastapi', 'fastapi'),
        ('real_benchmarks/django', 'django'),
        ('real_benchmarks/sqlalchemy', 'sqlalchemy'),
        ('real_benchmarks/celery', 'celery'),
    ]
    
    created_files = []
    for repo_path, repo_name in repos:
        if Path(repo_path).exists():
            csv_file, html_file = annotator.create_verification_samples(repo_path, repo_name, sample_size=30)
            if html_file:
                created_files.append(html_file)
                print(f"\n📝 To annotate {repo_name}:")
                print(f"   Open {html_file} in your browser")
    
    if created_files:
        print("\n" + "="*60)
        print("NEXT STEPS:")
        print("="*60)
        print("1. Open each HTML file in your browser")
        print("2. For each function call, verify if the callee exists")
        print("3. Click ✅ or ❌ for each call")
        print("4. Click 'Save & Export' when done")
        print("5. Run: python3 calculate_real_metrics.py")
        print("\nThis will give you HONEST precision numbers.")
    else:
        print("\n⚠️ No repositories found. Make sure you've cloned repos to real_benchmarks/")

if __name__ == "__main__":
    main()
