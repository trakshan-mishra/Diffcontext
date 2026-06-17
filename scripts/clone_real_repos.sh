#!/bin/bash

# Create directory for real repos
mkdir -p real_benchmarks
cd real_benchmarks

echo "📦 Cloning REAL production repositories..."

# GOOGLE (massive, complex)
echo "1. Google's TensorFlow (~2M lines, complex C++/Python)"
git clone --depth 1 https://github.com/tensorflow/tensorflow.git tensorflow 2>/dev/null || echo "Already exists"

echo "2. Google's JAX (functional programming, JIT compilation)"
git clone --depth 1 https://github.com/jax-ml/jax.git jax 2>/dev/null

# META
echo "3. Meta's PyTorch (~3M lines, complex build system)"
git clone --depth 1 https://github.com/pytorch/pytorch.git pytorch 2>/dev/null

echo "4. Meta's React (JavaScript/TypeScript mixed, hooks, components)"
git clone --depth 1 https://github.com/facebook/react.git react 2>/dev/null

# APPLE
echo "5. Apple's Swift (system programming, multiple modules)"
git clone --depth 1 https://github.com/apple/swift.git swift 2>/dev/null

# DEEPSEEK
echo "6. DeepSeek's Model (large language model architecture)"
git clone --depth 1 https://github.com/deepseek-ai/deepseek-model.git deepseek 2>/dev/null

# ANTHROPIC
echo "7. Anthropic's Claude SDK (API patterns, async, types)"
git clone --depth 1 https://github.com/anthropics/anthropic-sdk-python.git anthropic 2>/dev/null

# Additional challenging repos
echo "8. Microsoft's TypeScript (complex type system)"
git clone --depth 1 https://github.com/microsoft/TypeScript.git typescript 2>/dev/null

echo "9. Netflix's Conductor (workflow orchestration)"
git clone --depth 1 https://github.com/Netflix/conductor.git conductor 2>/dev/null

echo "10. Uber's Ludwig (declarative ML, complex inheritance)"
git clone --depth 1 https://github.com/uber/ludwig.git ludwig 2>/dev/null

echo "✅ Done cloning real-world repositories"
