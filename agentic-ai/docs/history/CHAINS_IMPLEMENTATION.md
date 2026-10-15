> Historical development notes from the original module build. Kept for reference; they are not current instructions. See ../../README.md.

# Chains Implementation in Module 2

## Overview

Successfully implemented **LangChain Chains** - a fundamental concept in the LangChain framework. Chains are composable sequences of operations that transform inputs into outputs in a deterministic, predefined way.

## What Was Added

### 1. Chain Implementations (`module2/chains/`)

Created three types of chains demonstrating different composition patterns:

#### Simple Chain
**Pattern**: `prompt → model → output_parser`

```python
from module2.chains import create_simple_analysis_chain

chain = create_simple_analysis_chain()
result = chain.invoke({
    "repo_path": "/mock/repo",
    "file_list": "package.json, Dockerfile, src/index.js"
})
```

**Use case**: Quick, single-step analysis with predictable output.

#### Multi-Step Chain
**Pattern**: `chain1 → chain2 → chain3` (sequential)

```python
from module2.chains import create_multi_step_analysis_chain

chain = create_multi_step_analysis_chain()
result = chain.invoke({
    "repo_path": "/mock/repo",
    "apps": ["api-service", "worker-service"],
    "dependencies": {"api": ["express", "pg"], "worker": ["celery", "redis"]}
})
```

**Use case**: Complex analysis where each step builds on previous results.

#### Parallel Chain
**Pattern**: `[chain1, chain2, chain3] → combine` (concurrent)

```python
from module2.chains import create_parallel_analysis_chain

chain = create_parallel_analysis_chain()
result = chain.invoke({
    "repo_info": "Multi-service Node.js and Python application"
})
```

**Use case**: Analyzing multiple aspects simultaneously (tech stack, security, scalability).

### 2. Demo Section (Section 4)

Added comprehensive demo section explaining:
- **Chains vs Agents**: When to use each
- **LCEL (LangChain Expression Language)**: The `|` operator for composition
- **Three chain types**: Simple, multi-step, parallel
- **Live chain execution**: Actually runs a simple chain with mock data
- **Decision criteria**: When to use chains vs agents

### 3. Key Concepts Demonstrated

#### LCEL (LangChain Expression Language)
```python
# The | operator chains components together
chain = prompt | model | output_parser

# Equivalent to:
# 1. prompt.invoke(input) → messages
# 2. model.invoke(messages) → ai_message
# 3. output_parser.invoke(ai_message) → string
```

#### Chains vs Agents

| Aspect | Chains | Agents |
|--------|--------|--------|
| **Workflow** | Fixed, deterministic | Dynamic, LLM-decided |
| **Speed** | Fast (fewer LLM calls) | Slower (multiple decisions) |
| **Cost** | Lower | Higher |
| **Use Case** | Known workflows | Complex reasoning |
| **Tool Selection** | Predefined | Dynamic |

#### When to Use Each

**Use Chains when**:
- Workflow is known and fixed
- Speed and cost are priorities
- No dynamic tool selection needed
- Example: "Always analyze dependencies → map to AWS → generate report"

**Use Agents when**:
- Workflow depends on data
- Need dynamic tool selection
- Complex multi-step reasoning required
- Example: "Explore repository and decide what to analyze based on findings"

### 4. Hybrid Approach

Module 2 uses **BOTH** chains and agents:
- **Chains**: For structured, known analysis steps
- **Agents**: For dynamic repository exploration

This combines the efficiency of chains with the flexibility of agents.

## Files Created/Modified

### New Files
- `module2/chains/__init__.py` - Chain module exports
- `module2/chains/analysis_chain.py` - Three chain implementations (~250 lines)

### Modified Files
- `demos/module2_demo.py` - Added Section 4 (chains concept)
- `README.md` - Updated to reflect 9 sections

## Demo Structure Update

The demo now has **9 sections** (previously 8):

1. Framework Comparison
2. Agent Identity & System Prompts
3. Context Management
4. **Chains: Composable Workflows** ✨ NEW
5. Repository Structure Scan
6. Multi-Application Detection
7. Stack Analysis & AWS Service Mapping
8. LangSmith Tracing
9. Complete Analysis Pipeline

## Running the Chains Demo

```bash
# Run just the chains section
AGENT_MOCK_REPO=true python demos/module2_demo.py --section 4

# Run all sections
AGENT_MOCK_REPO=true python demos/module2_demo.py
```

## Code Examples

### Simple Chain Implementation
```python
def create_simple_analysis_chain(region: str | None = None) -> Runnable:
    # Step 1: Define the prompt template
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM_PROMPT),
        ("human", "Analyze this repository: {repo_path}\nFiles: {file_list}")
    ])
    
    # Step 2: Get the model
    model = get_chat_bedrock_model(region=region)
    
    # Step 3: Define output parser
    output_parser = StrOutputParser()
    
    # Step 4: Compose the chain using LCEL
    chain = prompt | model | output_parser
    
    return chain
```

### Multi-Step Chain Implementation
```python
def create_multi_step_analysis_chain(region: str | None = None) -> Runnable:
    model = get_chat_bedrock_model(region=region)
    output_parser = StrOutputParser()
    
    # First chain: analyze dependencies
    dependency_chain = dependency_prompt | model | output_parser
    
    # Second chain: create summary (uses output of first chain)
    summary_chain = (
        {"dependency_analysis": dependency_chain}
        | summary_prompt
        | model
        | output_parser
    )
    
    return summary_chain
```

### Parallel Chain Implementation
```python
def create_parallel_analysis_chain(region: str | None = None) -> Runnable:
    # Create three parallel chains
    tech_chain = tech_prompt | model | output_parser
    security_chain = security_prompt | model | output_parser
    scalability_chain = scalability_prompt | model | output_parser
    
    # Combine results
    parallel_chain = (
        {
            "tech_analysis": tech_chain,
            "security_analysis": security_chain,
            "scalability_analysis": scalability_chain,
        }
        | combine_prompt
        | model
        | output_parser
    )
    
    return parallel_chain
```

## Educational Value

This implementation demonstrates:

1. **Fundamental LangChain Concept**: Chains are the building block of LangChain applications
2. **LCEL Syntax**: The declarative `|` operator for composition
3. **Composition Patterns**: Simple, sequential, and parallel
4. **Practical Use Cases**: When chains are better than agents
5. **Hybrid Architecture**: Combining chains and agents for optimal results

## Benefits

1. **Performance**: Chains are faster than agents for known workflows
2. **Cost**: Fewer LLM calls = lower costs
3. **Predictability**: Deterministic execution path
4. **Composability**: Easy to build complex workflows from simple components
5. **Testability**: Each chain component can be tested independently

## Integration with Existing Code

The chains complement the existing agent-based approach:
- **Agents** (`module2/agent.py`): Dynamic tool selection for exploration
- **Chains** (`module2/chains/`): Structured workflows for known analysis steps
- **Tools** (`module2/tools/`): Shared between chains and agents
- **Prompts** (`module2/prompts/`): Reused in chain templates

## Next Steps

The chains implementation sets the foundation for:
- **Module 3**: Evaluation and routing (using chains for routing logic)
- **Module 4**: Multi-agent orchestration (chains for coordination)
- **Advanced patterns**: Chain-of-thought, self-ask, ReAct chains

## Summary

Successfully added a complete chains implementation to Module 2, including:
- ✅ Three chain types (simple, multi-step, parallel)
- ✅ Comprehensive demo section with live execution
- ✅ Clear explanation of chains vs agents
- ✅ LCEL syntax demonstration
- ✅ Practical use case guidance
- ✅ Integration with existing agent architecture

The demo now provides a complete educational journey through LangChain's core concepts: prompts, context, chains, and agents.
