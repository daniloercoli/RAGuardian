"""
Wrapper script executed by the container. The application validates user imports
before launching this script; the wrapper captures output and generated images.
"""
import json
import os
import sys
import warnings


def _link_data_files(data_dir, work_dir):
    """Expose /data files by filename from the writable working directory."""
    os.makedirs(work_dir, exist_ok=True)
    for filename in os.listdir(data_dir):
        if filename == "run.py":
            continue
        src = os.path.join(data_dir, filename)
        dst = os.path.join(work_dir, filename)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue
        try:
            os.symlink(src, dst)
        except OSError:
            pass


def main():
    if len(sys.argv) != 3:
        sys.stderr.write("Usage: subprocess_wrapper.py <code.py> <result.json>\n")
        sys.exit(2)

    code_path = sys.argv[1]
    result_path = sys.argv[2]

    # Read code
    with open(code_path, encoding="utf-8") as f:
        code = f.read()

    run_dir = os.path.dirname(code_path)
    image_dir = os.environ.get("IMAGE_OUTPUT_DIR", run_dir)
    image_prefix = os.path.splitext(os.path.basename(code_path))[0]
    _link_data_files(run_dir, image_dir)
    os.chdir(image_dir)

    # stdout buffer
    class TextBuffer:
        def __init__(self):
            self.text = ""

        def write(self, s):
            self.text += s

        def writelines(self, lines):
            self.text += "".join(map(str, lines))

        def flush(self):
            pass

        def isatty(self):
            return False

    buf = TextBuffer()
    old = sys.stdout
    sys.stdout = buf

    collected_images = []

    try:
        # Execute user code
        exec(compile(code, "run.py", "exec"), {"__name__": "__main__"})

        # Auto-save any open figure
        plt = sys.modules.get("matplotlib.pyplot")
        if plt is not None:
            active = plt.gcf()
            if active.get_axes():
                out_name = os.path.join(image_dir, f"{image_prefix}.png")
                plt.savefig(out_name, dpi=150, bbox_inches="tight")
                collected_images.append(out_name)
                plt.close()
    except Exception as exc:
        sys.stdout = old
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(
                {"success": False, "error": str(exc), "text": buf.text, "images": []},
                f,
            )
        return

    sys.stdout = old

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(
            {"success": True, "text": buf.text, "images": collected_images},
            f,
        )


if __name__ == "__main__":
    main()
