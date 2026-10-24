# Go2 MCP integration contract

This contract connects the existing `is_this_us` backend to
[`zhaw-physical-ai/go2_main`](https://github.com/zhaw-physical-ai/go2_main/tree/6bca98f7144a104b065e73f20aa2b2cd88ebcb3f).
It supports the [implementation plan](IMPLEMENTATION_PLAN.md). Sections marked as required
extensions describe work to implement; they are not tools supplied by the current upstream.

## Current transport and tools

The inspected [`go2_mcp/server.py`](https://github.com/zhaw-physical-ai/go2_main/blob/6bca98f7144a104b065e73f20aa2b2cd88ebcb3f/go2_mcp/server.py)
runs a FastMCP server using **stdio**, not an HTTP MCP URL. It opens ZMQ request sockets to
`GO2_BRIDGE_HOST:GO2_ZMQ_CMD_PORT` and a camera subscriber to `GO2_ZMQ_PUB_PORT`. Defaults are
`localhost`, `5555`, and `5556`. Both receive paths currently use a five-second timeout.

| MCP tool | Arguments | Current result | Backend use |
| --- | --- | --- | --- |
| `get_status` | none | Text `OK: ...` followed by JSON for obstacle avoidance, speed level, and light state. | Bridge connectivity/configuration only. Does not prove robot state freshness or posture. |
| `list_actions` | none | Text with JSON `actions` list. | Discover names and intersect with the qualified profile. Registration does not prove firmware support. |
| `get_camera_frame` | none | `CallToolResult` with text and base64 JPEG `ImageContent`; timeout is a text-only `ERROR`. | Decode a bounded image in memory; require an image block, not merely absence of `isError`. |
| `execute_action` | `name: str` | Text `OK/ERROR` plus JSON containing SDK `code` where available. | Dispatch exactly one allowlisted action after guard approval. |
| `stop` | none | Text response to bridge `stop`. | Best-effort velocity stop; see interruption limits below. |
| `set_obstacle_avoidance` | `enabled: bool` | Text response. | Operator setup only; retain enabled configuration. |
| `set_speed_level` | `level: int` | Text response. | Operator setup only; no visitor or model access. |
| `set_light` | `on: bool` | Text and SDK code. | Optional expression after the main loop works. |
| `move` | `vx`, `vy=0`, `vyaw=0` | Text response. | Do not expose through the installation adapter; no walking in this version. |

Use the [official MCP Python client](https://github.com/modelcontextprotocol/python-sdk) with
`StdioServerParameters`, `stdio_client`, and `ClientSession`; initialize the session, discover
tools, and validate their schemas before enabling the adapter. MCP calls can come from ordinary
Python code; an LLM is not needed to operate the protocol.

The backend owns one persistent MCP process/session. Keep its event loop in a dedicated worker
to bridge the current synchronous `RobotAdapter` and `FrameSource` interfaces; callers submit
coroutines using a thread-safe boundary. Never share asyncio primitives across loops or invoke
`asyncio.run()` separately for every tool call. Keep diagnostics on stderr so stdout remains
valid MCP traffic. Close streams, sockets, and the subprocess on shutdown.

## Startup configuration

On the host where Unitree SDK2 already works, this is an existing upstream command:

```bash
cd /path/to/go2_main
GO2_NETWORK_INTERFACE=<working_interface> python -m go2_bridge
```

The backend will launch the MCP subprocess using the absolute Python interpreter path and
working directory of a pinned `go2_main` checkout. It passes `GO2_BRIDGE_HOST`,
`GO2_ZMQ_CMD_PORT`, and `GO2_ZMQ_PUB_PORT` to that process. Record the resolved upstream
commit and MCP package version in diagnostics.

Proposed backend settings to add to `Settings` and `.env.example`:

```dotenv
# Existing settings retained
APP_MODE=installation
ROBOT_MODEL=go2
ROBOT_MOTION_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_OPERATOR_IDS=

# Proposed settings and camera-source value; not implemented yet
ROBOT_TRANSPORT=mcp
CAMERA_SOURCE=mcp
GO2_MCP_PYTHON=/absolute/path/to/python
GO2_MCP_CWD=/absolute/path/to/go2_main
GO2_BRIDGE_HOST=127.0.0.1
GO2_ZMQ_CMD_PORT=5555
GO2_ZMQ_PUB_PORT=5556
MCP_RPC_TIMEOUT_SECONDS=6
ROBOT_PROFILE_PATH=./config/robot_profiles/go2_local.yaml
OBSERVATION_MODE=manual
```

Use a six-second client deadline initially so it can receive the current five-second upstream
timeout response. This is not an emergency-stop latency target. The camera freshness gate and
motion telemetry age limit are separate. Keep the current simulation defaults for a clean
checkout. Add `ROBOT_TRANSPORT=direct_sdk` as an explicit alternate, with no automatic fallback
from a failed MCP session into a second robot controller.

## Action mapping and transition qualification

The upstream
[`ACTION_REGISTRY`](https://github.com/zhaw-physical-ai/go2_main/blob/6bca98f7144a104b065e73f20aa2b2cd88ebcb3f/go2_bridge/protocol.py)
exposes high-level routines, not joint targets. Candidate mappings are:

| Backend behavior | MCP action names | Requirement |
| --- | --- | --- |
| `stillness` | No action | Means no new motion; it is not a stop command. |
| `stand` | `stand_up`, `balance_stand` as appropriate | Choose the transition from a known measured starting body state. |
| `sit` | `sit` after a qualified standing preparation | Confirm seated completion and clearance on the actual unit. |
| `lie_down` | `stand_down` after qualified preparation | Treat as an approximation of lying; qualify entry and exit. |
| `greet` | `hello` after qualified standing preparation | Optional; qualify movement envelope and interruption first. |
| `stretch` | `stretch` after qualified standing preparation | Optional; qualify movement envelope and interruption first. |

**`RiseSit` is absent from MCP.** The existing `robot/base.py::steps_for` uses it for sit-to-stand
and sit-to-lie transitions. Preferred upstream extension: register `rise_sit` for SDK `RiseSit`,
then qualify that transition on this unit. Until it is available and tested, profiles requiring
it stay disabled. Do not silently substitute `stand_up` for `RiseSit`. An operator may reset
the unit between isolated tests, but that is not an autonomous transition implementation.

Define a versioned hardware profile with robot/firmware identity, upstream commit, qualified
actions, allowed `(start_state, behavior)` transitions, measured settle durations, maximum
durations, completion criteria, and tested interruption behavior. A planned sequence from
`unknown` body state must be rejected for real hardware; the current SDK helper's automatic
stand-up from `UNKNOWN` is not adopted as the MCP default.

The executable set is the intersection of registered actions, the local allowlist, and the
qualified profile. Explicitly exclude walking, flips, jumps, dances, handstands, `damp`, and
automatic recovery routines from visitor interactions. Treat robot-side SDK return codes as
authoritative for acceptance, not action names or natural-language response wording.

## Required extensions before enabling motion

### Structured results and fresh telemetry

The current
[`command_handler.py`](https://github.com/zhaw-physical-ai/go2_main/blob/6bca98f7144a104b065e73f20aa2b2cd88ebcb3f/go2_bridge/command_handler.py)
returns `ok: true` for an action whenever the SDK method returns without raising, including
a nonzero SDK code. Fix this to report failure for nonzero codes and expose structured MCP
results while preserving existing text clients if needed. Before that extension, the backend
must parse the known pinned text format and reject missing, malformed, or nonzero codes.
`CallToolResult.isError == false` alone is insufficient.

Add a read-only telemetry subscriber on the **bridge host**, using the working SDK state
topics, and expose it through status/MCP. Required fields:

```json
{
  "schema_version": 1,
  "bridge_instance_id": "example-boot-uuid",
  "robot_connected": true,
  "state_sequence": 42,
  "state_age_ms": 120,
  "battery_percent": 72,
  "error_code": 0,
  "body_state": "standing",
  "body_state_source": "telemetry_mapping",
  "active_action_id": null
}
```

This JSON is an illustrative target contract, not a current response. Compute `state_age_ms`
from monotonic time on the bridge; add request round-trip time conservatively in the backend.
Track sequence advancement and boot identity to detect frozen or restarted sources. Use a
starting stale-state cutoff of one second and the existing 25% minimum battery threshold;
calibrate the freshness cutoff against measured topic rates. Do not fabricate battery values
to bypass the current guard, which correctly blocks real motion with an unknown battery.

Map firmware state fields to body states only after validation. If telemetry cannot distinguish
a posture, report `unknown` or use an explicit operator-confirmed supervised mode with a
short-lived confirmation. A last commanded posture is not a measured posture. Full automatic
operation requires reliable state/completion evidence; operator-confirmed mode is a reduced
demonstration capability and must be labeled as such.

Add action identifiers and result states such as `accepted`, `completed`, `failed`, and
`unknown`. SDK return code zero establishes acceptance, not physical completion. Completion
requires tested state evidence, or an explicit operator confirmation for supervised trials.
Elapsed settling time alone must be recorded as an estimate, not proof. Prefer a bridge-side
request ID deduplication ledger; until then, never retry an action whose reply was lost.

### Stop and cancellation behavior

The current bridge `stop` zeroes the velocity loop and calls `Move(0,0,0)`. The separate
`stop_move` action calls SDK `StopMove`. Neither is proof that every named routine can be
interrupted. The [movement watchdog](https://github.com/zhaw-physical-ai/go2_main/blob/6bca98f7144a104b065e73f20aa2b2cd88ebcb3f/go2_bridge/movement_loop.py)
expires velocity commands after 250 ms; it does **not** cancel a named action sequence.

The command handler dispatches synchronously on one REP socket, so a stop request can wait
behind an in-progress SDK call. An extra MCP client socket alone cannot fix that. Implement
bridge-side cancellation/priority handling with an SDK-safe control design, and measure its
latency during each permitted routine. Keep the physical remote/stop procedure available to
the operator, as required by the existing [hardware checklist](SAFETY_CHECKLIST.md).

At the application layer, `/stop`, `/disarm`, session release, and camera loss before dispatch
invalidate the pending execution generation. Check it before every sequence step and during
settling. A stop latches the application disarmed; reconnecting does not re-arm. If the current
routine cannot be interrupted, issue the validated best-effort stop, flag the unresolved action,
and send no further steps. Do not automatically command a stand-up recovery on failure.

Keep stop handling independent of slow capture, inference, captions, and delivery. Cancelled
or timed-out worker futures may still execute code: retain the motion lock until the worker
has actually exited, and prevent an old worker from resuming after a new arm operation.

## Camera freshness and throughput

The current `get_camera_frame` creates a subscriber, waits for one `camera` message, and returns
JPEG content without a robot capture timestamp. Record receive time and acquisition duration;
do not label receive time as capture time. Reject late acquisitions and test frozen-stream
detection. Exact frame freshness needs upstream sequence/capture metadata.

For continuous observation, prefer a single persistent subscriber to the existing two-part
`[b"camera", jpeg_bytes]` stream if MCP snapshots are too slow. Decode only the newest frame;
bound buffering and drop older complete multipart messages. Motion commands still use MCP.
This is an explicit camera optimization, not a change to the robot control path.

Add versioned camera metadata with frame sequence and monotonic age without breaking existing
two-part subscribers, for example via a separate versioned topic. Drain stale buffers, bound
JPEG size and decoded pixel count, validate MIME type and dimensions, and handle corrupt bytes.
Keep raw visitor frames in memory. An upstream dummy camera cannot establish real frame rate,
visibility, or image freshness on the Go2.

## Compatibility and acceptance checks

Build MCP fixtures from the pinned source for successful results, text-only errors, nonzero
SDK codes inside `OK` messages, malformed JSON, missing image blocks, and timeouts. Add a
read-only smoke check against the real bridge before any motion test. The existing upstream
`dummy_server.py` can exercise transport wiring but is not a physics or telemetry simulator.

Before releasing a hardware profile, record a test for every allowed starting state and
behavior, observed terminal state, SDK code, duration, and stop behavior. Verify bridge loss,
application restart, stale telemetry, and duplicate request handling. Then update
`HARDWARE_SETUP.md`, `COMPATIBILITY.md`, and `SAFETY_CHECKLIST.md` to describe the actual selected
transport; their current instructions concern the direct SDK adapter.

If upstream changes cannot be merged promptly, pin a reviewed fork/patch of `go2_main` and
record its commit in the profile. Do not copy an untracked variant into the application. Until
the required motion contract is met, the MCP camera, local matching, mock execution, and
Telegram delivery can still be integrated and demonstrated.
