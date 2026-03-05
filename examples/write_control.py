import argparse
import asyncio
import json

from livepeer_gateway.control import ControlConfig, ControlMode
from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.lv2v import StartJobRequest, start_lv2v


DEFAULT_MODEL_ID = "noop"  # fix


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Start an LV2V job and write JSON messages to its control channel.")
    p.add_argument(
        "orchestrator",
        nargs="?",
        default=None,
        help="Orchestrator (host:port). If omitted, discovery is used.",
    )
    p.add_argument(
        "--signer",
        default=None,
        help="Remote signer URL (no path). If omitted, runs in offchain mode.",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL_ID,
        help=f"Pipeline model to start via /live-video-to-video. Default: {DEFAULT_MODEL_ID}",
    )
    p.add_argument(
        "--message",
        default='{"type":"ping"}',
        help='JSON object to send on the control channel (default: {"type":"ping"}).',
    )
    p.add_argument("--count", type=int, default=1, help="How many times to send the message (default: 1).")
    p.add_argument("--interval", type=float, default=0.2, help="Seconds between messages (default: 0.2).")
    p.add_argument(
        "--mode",
        choices=[ControlMode.MESSAGE.value, ControlMode.TIME.value],
        default=ControlMode.MESSAGE.value,
        help="Control channel mode: message or time (default: message).",
    )
    p.add_argument(
        "--segment-interval",
        type=float,
        default=10.0,
        help="Rotation interval in seconds for time mode (default: 10.0).",
    )
    return p.parse_args()


async def main() -> None:
    args = _parse_args()

    try:
        control_config = ControlConfig(
            mode=ControlMode(args.mode),
            segment_interval=args.segment_interval,
        )
        job = start_lv2v(
            args.orchestrator,
            StartJobRequest(model_id=args.model),
            signer_url=args.signer,
            control_config=control_config,
        )

        print("=== LiveVideoToVideo ===")
        print("control_url:", job.control_url)
        print()

        if not job.control:
            raise LivepeerGatewayError("No control_url present on this LiveVideoToVideo job")

        msg = json.loads(args.message)
        if not isinstance(msg, dict):
            raise ValueError("--message must be a JSON object")

        for i in range(max(0, args.count)):
            await job.control.write({**msg, "n": i})
            if i + 1 < args.count:
                await asyncio.sleep(args.interval)

    except LivepeerGatewayError as e:
        print(f"ERROR: {e}")
    finally:
        try:
            if "job" in locals():
                await job.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())


