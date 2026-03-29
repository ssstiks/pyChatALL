# ARCHITECTURE AND PROJECT RULES
(Briefly describe the project here, e.g., "Python scripts for network infrastructure automation, API interaction, and system administration.")

# STRICT CONTEXT ECONOMY RULES (ALWAYS ENFORCE)

## 1. NAVIGATION AND CODE SEARCH
DO NOT use standard bash utilities like `cat`, `grep`, `find`, or `less` for initial codebase exploration. Reading entire files causes severe context bloat.
For navigation, you MUST use the `qmd` MCP tools:
- Use `mcp__qmd__vector_search` or `mcp__qmd__search` to locate specific functions and logic.
- Use `mcp__qmd__deep_search` to analyze dependencies.
- Extract only the necessary code snippets using `mcp__qmd__get` / `mcp__qmd__multi_get`. Never read a whole file if you only need a single function or class.

## 2. LIBRARY DOCUMENTATION
Do not hallucinate or guess API parameters, library syntax, or network utility flags.
Before using new modules or libraries, always consult the `context7` MCP:
- Call `mcp__context7__resolve-library-id` and `mcp__context7__query-docs` to load the exact, up-to-date specification directly into your prompt.

## 3. AUTONOMOUS EXECUTION AND VERIFICATION
You have the `verification-before-completion` and `test-driven-development` skills active.
Never stop or ask the user for confirmation if the code has not been verified:
1. Write the code.
2. Autonomously run syntax checks (e.g., `ruff`, `flake8`, or run the script with `--help`/`--dry-run`).
3. Read the terminal output.
4. If there are errors or tracebacks, fix them within the same autonomous loop.
5. Report back only upon a successful result.

## 4. TASK PLANNING
- For complex tasks: You must use the `superpowers:write-plan` and `superpowers:execute-plan` skills. Break the task down, save the plan to a local `.md` file, and follow it step-by-step.
- For minor edits (under 20 lines of code): Bypass the planning phase and execute the task immediately to save tokens.

## 5. PYTHON CODING STANDARDS
- **Typing:** Strictly use Type Hints for all functions, methods, and return values.
- **Network Stability:** When writing scripts that interact with external APIs or system daemons, implement robust timeout handling and network error catching (`try/except` blocks).
- **Logging:** No `print()` statements for debugging in the final code. Use the standard `logging` module with clear levels (INFO, ERROR, DEBUG). Code must be ready to run as a background service.
- **Style:** Adhere to PEP 8 standards. Ensure linting tools pass without errors.

## 6. ISOLATION AND GIT WORKTREES
You have the `using-git-worktrees` and `finishing-a-development-branch` skills active. Strictly follow this workflow:
- For developing any new feature or refactoring, **it is forbidden** to work directly in the `main` or `master` branch.
- You must activate the `using-git-worktrees` skill and create an isolated working tree for the current task. This prevents the main directory from being cluttered with temporary files and logs that bloat the context.
- Once the code is written and verified (per Rule 3), use the `finishing-a-development-branch` skill to cleanly merge the changes and automatically delete the temporary worktree.
