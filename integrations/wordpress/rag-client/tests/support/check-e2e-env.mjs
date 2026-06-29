import {execFileSync} from "node:child_process";

function run(command, args) {
    try {
        return {
            ok: true,
            output: execFileSync(command, args, {
                encoding: "utf8",
                stdio: ["ignore", "pipe", "pipe"],
            }).trim(),
        };
    } catch (error) {
        return {
            ok: false,
            output: [
                error.stdout?.toString().trim(),
                error.stderr?.toString().trim(),
                error.message,
            ].filter(Boolean).join("\n"),
        };
    }
}

const checks = [
    {
        name: "Docker CLI",
        result: run("docker", ["--version"]),
        hint: "Install Docker Desktop for macOS, or make sure docker is in PATH.",
    },
    {
        name: "Docker Compose V2",
        result: run("docker", ["compose", "version"]),
        hint: "wp-env requires `docker compose`. On macOS, install and start Docker Desktop with `brew install --cask docker`.",
    },
    {
        name: "Docker daemon",
        result: run("docker", ["info"]),
        hint: "Open Docker Desktop and wait until it says Docker is running.",
    },
];

let failed = false;
checks.forEach((check) => {
    if (check.result.ok) {
        console.log(`OK ${check.name}: ${check.result.output.split("\n")[0]}`);
        return;
    }
    failed = true;
    console.error(`FAIL ${check.name}`);
    console.error(check.result.output);
    console.error(`Hint: ${check.hint}`);
});

if (failed) {
    console.error("\nE2E environment is not ready.");
    process.exit(1);
}
