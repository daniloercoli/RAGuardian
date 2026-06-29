import http from "node:http";
import {Buffer} from "node:buffer";

const port = Number(process.env.FAKE_RAG_PORT || 5055);
const requests = [];

function json(response, status, payload) {
    const body = JSON.stringify(payload);
    response.writeHead(status, {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
    });
    response.end(body);
}

function readBody(request) {
    return new Promise((resolve, reject) => {
        const chunks = [];
        request.on("data", (chunk) => chunks.push(chunk));
        request.on("end", () => resolve(Buffer.concat(chunks)));
        request.on("error", reject);
    });
}

function parseMultipart(buffer, contentType) {
    const boundaryMatch = /boundary=([^;]+)/i.exec(contentType || "");
    if (!boundaryMatch) return {fields: {}, file: null};

    const boundary = "--" + boundaryMatch[1];
    const raw = buffer.toString("binary");
    const fields = {};
    let file = null;
    raw.split(boundary).forEach((part) => {
        const trimmed = part.replace(/^\r\n/, "").replace(/\r\n--$/, "").replace(/\r\n$/, "");
        if (!trimmed || trimmed === "--") return;
        const splitAt = trimmed.indexOf("\r\n\r\n");
        if (splitAt === -1) return;
        const headerText = trimmed.slice(0, splitAt);
        const bodyBinary = trimmed.slice(splitAt + 4);
        const nameMatch = /name="([^"]+)"/i.exec(headerText);
        if (!nameMatch) return;
        const filenameMatch = /filename="([^"]*)"/i.exec(headerText);
        const contentTypeMatch = /content-type:\s*([^\r\n]+)/i.exec(headerText);
        const name = nameMatch[1];
        const body = Buffer.from(bodyBinary, "binary");
        if (filenameMatch) {
            file = {
                field: name,
                filename: filenameMatch[1],
                contentType: contentTypeMatch ? contentTypeMatch[1] : "",
                text: body.toString("utf8"),
                size: body.length,
            };
        } else {
            fields[name] = body.toString("utf8");
        }
    });
    return {fields, file};
}

function record(request, payload) {
    requests.push({
        id: requests.length + 1,
        method: request.method,
        path: new URL(request.url, `http://${request.headers.host}`).pathname,
        search: new URL(request.url, `http://${request.headers.host}`).search,
        apiKey: request.headers["x-api-key"] || "",
        ...payload,
    });
}

const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, `http://${request.headers.host}`);

    if (url.pathname === "/__health") {
        json(response, 200, {ok: true});
        return;
    }
    if (url.pathname === "/__reset" && request.method === "POST") {
        requests.splice(0, requests.length);
        json(response, 200, {ok: true});
        return;
    }
    if (url.pathname === "/__requests") {
        json(response, 200, {requests});
        return;
    }

    if (url.pathname === "/api/v1/health" && request.method === "GET") {
        record(request, {type: "health"});
        json(response, 200, {
            status: "healthy",
            system_ready: true,
            documents_count: requests.filter((entry) => entry.type === "file_upload").length,
            queue_backend: "redis",
            queue_ready: true,
        });
        return;
    }

    if (url.pathname === "/api/v1/query" && request.method === "POST") {
        const body = JSON.parse((await readBody(request)).toString("utf8") || "{}");
        record(request, {type: "query", body});
        json(response, 200, {
            answer: `Fake RAG answer for: ${body.query || ""}`,
            conversation_id: body.conversation_id || "",
            sources: [
                {
                    filename: "wordpress/posts/post-101.txt",
                    source_type: "text",
                    snippet: "Fixture public article",
                },
            ],
        });
        return;
    }

    if (url.pathname === "/api/v1/files" && request.method === "POST") {
        const body = await readBody(request);
        const multipart = parseMultipart(body, request.headers["content-type"]);
        record(request, {type: "file_upload", multipart});
        json(response, 202, {
            job_id: `job-${requests.length}`,
            type: "file_upload",
            status: "queued",
            filename: multipart.fields.relative_path || multipart.file?.filename || "document.txt",
            result: null,
        });
        return;
    }

    if (url.pathname === "/api/v1/audio" && request.method === "POST") {
        const body = await readBody(request);
        const multipart = parseMultipart(body, request.headers["content-type"]);
        record(request, {type: "audio_upload", multipart});
        json(response, 202, {
            job_id: `audio-job-${requests.length}`,
            type: "audio_upload",
            status: "queued",
            result: null,
        });
        return;
    }

    if (url.pathname === "/api/v1/tts" && request.method === "POST") {
        const body = JSON.parse((await readBody(request)).toString("utf8") || "{}");
        record(request, {type: "tts", body});
        const audio = Buffer.from("fake-audio");
        response.writeHead(200, {
            "Content-Type": "audio/mpeg",
            "Content-Length": audio.length,
        });
        response.end(audio);
        return;
    }

    if (url.pathname.startsWith("/api/v1/files/") && request.method === "DELETE") {
        record(request, {
            type: "file_delete",
            filename: decodeURIComponent(url.pathname.replace("/api/v1/files/", "")),
        });
        json(response, 200, {deleted: true});
        return;
    }

    json(response, 404, {error: "not found"});
});

server.listen(port, "0.0.0.0", () => {
    console.log(`Fake RAG server listening on ${port}`);
});

process.on("SIGTERM", () => server.close(() => process.exit(0)));
process.on("SIGINT", () => server.close(() => process.exit(0)));
