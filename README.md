# Net2Board

A Python engine that models the physical realization of a circuit — bridging the gap between netlist and circuit layout. Built as a library for an external AI orchestrator to drive placement, routing, DRC, and iterative optimization.

## Ambition

1. AI-orchestrated placement and routing of imported netlists
2. Pluggable simulations (SI, PI, thermal, EM), feeding violations back for iterative improvement
3. Support for additional EDA formats beyond KiCad
4. Integration with external AI orchestrator tooling (MCP, agent loops)
