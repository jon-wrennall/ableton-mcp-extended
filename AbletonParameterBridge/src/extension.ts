import {
  initialize,
  MidiTrack,
  AudioTrack,
  type ActivationContext,
} from "@ableton-extensions/sdk";
import * as http from "http";
import * as urlModule from "url";

const HTTP_PORT = 9878;

export function activate(activation: ActivationContext) {
  const api = initialize(activation, "1.0.0");

  console.log("[ParameterBridge] Starting HTTP server on port " + HTTP_PORT);

  const server = http.createServer(async (req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Access-Control-Allow-Origin", "*");

    try {
      const parsed = urlModule.parse(req.url!, true);
      const pathname = parsed.pathname ?? "/";
      const searchParams = parsed.query as Record<string, string>;
      // pathname already set above

      // GET /tracks — list all tracks and their devices with param counts
      if (req.method === "GET" && pathname === "/tracks") {
        const song = api.application.song;
        const tracks = song.tracks;
        const result = tracks.map((track, i) => ({
          index: i,
          name: track.name,
          type: track instanceof MidiTrack ? "midi" : "audio",
          devices: track.devices.map((device, j) => ({
            index: j,
            name: device.name,
            param_count: device.parameters.length,
          })),
        }));
        res.writeHead(200);
        res.end(JSON.stringify({ tracks: result }));
        return;
      }

      // GET /params?track=0&device=0 — get all parameters for a device
      if (req.method === "GET" && pathname === "/params") {
        const trackIndex = parseInt(searchParams["track"] ?? "0");
        const deviceIndex = parseInt(searchParams["device"] ?? "0");

        const song = api.application.song;
        const tracks = song.tracks;
        if (trackIndex < 0 || trackIndex >= tracks.length) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Track index out of range" }));
          return;
        }
        const track = tracks[trackIndex];
        const devices = track.devices;
        if (deviceIndex < 0 || deviceIndex >= devices.length) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Device index out of range" }));
          return;
        }
        const device = devices[deviceIndex];
        const params = device.parameters;

        const paramData = await Promise.all(
          params.map(async (p, i) => {
            try {
              const value = await p.getValue();
              return {
                index: i,
                name: p.name,
                value,
                min: p.min,
                max: p.max,
                is_quantized: p.isQuantized,
                default_value: p.defaultValue,
                value_items: p.isQuantized ? p.valueItems.map((v) => v.name) : [],
              };
            } catch (e) {
              return {
                index: i,
                name: p.name,
                error: String(e),
                value: 0,
                min: 0,
                max: 1,
                is_quantized: false,
                default_value: null,
                value_items: [],
              };
            }
          })
        );

        res.writeHead(200);
        res.end(
          JSON.stringify({
            track_index: trackIndex,
            track_name: track.name,
            device_index: deviceIndex,
            device_name: device.name,
            parameters: paramData,
          })
        );
        return;
      }

      // POST /params — set a parameter value
      // Body: { track: 0, device: 0, param_index?: 0, param_name?: "Filter", value: 64 }
      if (req.method === "POST" && pathname === "/params") {
        const body = await readBody(req);
        const { track: trackIndex, device: deviceIndex, param_index, param_name, value } =
          JSON.parse(body);

        const song = api.application.song;
        const track = song.tracks[trackIndex];
        const device = track.devices[deviceIndex];
        const params = device.parameters;

        let param = null;
        if (param_index !== undefined && param_index !== null) {
          param = params[param_index] ?? null;
        } else if (param_name) {
          param =
            params.find((p) => p.name.toLowerCase() === String(param_name).toLowerCase()) ?? null;
        }

        if (!param) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Parameter not found" }));
          return;
        }

        const clamped = Math.max(param.min, Math.min(param.max, Number(value)));
        await param.setValue(clamped);
        const newValue = await param.getValue();

        res.writeHead(200);
        res.end(JSON.stringify({ param_name: param.name, value: newValue }));
        return;
      }

      // GET /snapshot?track=0&device=0 — capture all values
      if (req.method === "GET" && pathname === "/snapshot") {
        const trackIndex = parseInt(searchParams["track"] ?? "0");
        const deviceIndex = parseInt(searchParams["device"] ?? "0");
        const song = api.application.song;
        const track = song.tracks[trackIndex];
        const device = track.devices[deviceIndex];
        const params = device.parameters;
        const snapshot: Record<string, number> = {};
        for (const p of params) {
          try {
            snapshot[p.name] = await p.getValue();
          } catch (_) {
            // skip unreadable params
          }
        }
        res.writeHead(200);
        res.end(
          JSON.stringify({
            track_index: trackIndex,
            device_index: deviceIndex,
            device_name: device.name,
            snapshot,
          })
        );
        return;
      }

      // POST /snapshot — bulk restore: set all params from a saved snapshot dict
      // Body: { track: 0, device: 0, params: { "Filter Freq": 0.8, "Resonance": 0.3, ... } }
      if (req.method === "POST" && pathname === "/snapshot") {
        const body = await readBody(req);
        const { track: trackIndex, device: deviceIndex, params: paramValues } = JSON.parse(body);

        const song = api.application.song;
        const track = song.tracks[trackIndex];
        const device = track.devices[deviceIndex];
        const liveParams = device.parameters;

        const results: { name: string; value: number; status: string }[] = [];

        await Promise.all(
          Object.entries(paramValues as Record<string, number>).map(async ([name, value]) => {
            const param = liveParams.find(
              (p) => p.name.toLowerCase() === name.toLowerCase()
            );
            if (!param) {
              results.push({ name, value, status: "not_found" });
              return;
            }
            try {
              const clamped = Math.max(param.min, Math.min(param.max, Number(value)));
              await param.setValue(clamped);
              results.push({ name, value: clamped, status: "ok" });
            } catch (e) {
              results.push({ name, value, status: `error: ${String(e)}` });
            }
          })
        );

        const applied = results.filter((r) => r.status === "ok").length;
        res.writeHead(200);
        res.end(
          JSON.stringify({
            track_index: trackIndex,
            device_index: deviceIndex,
            device_name: device.name,
            applied,
            total: Object.keys(paramValues).length,
            results,
          })
        );
        return;
      }

      // GET /health — liveness check
      if (req.method === "GET" && pathname === "/health") {
        res.writeHead(200);
        res.end(JSON.stringify({ status: "ok", port: HTTP_PORT }));
        return;
      }

      res.writeHead(404);
      res.end(JSON.stringify({ error: "Not found" }));
    } catch (e) {
      console.error("[ParameterBridge] Error:", e);
      res.writeHead(500);
      res.end(JSON.stringify({ error: String(e) }));
    }
  });

  server.listen(HTTP_PORT, "127.0.0.1", () => {
    console.log("[ParameterBridge] HTTP server listening on port " + HTTP_PORT);
  });

  return () => {
    server.close();
    console.log("[ParameterBridge] HTTP server stopped");
  };
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}
