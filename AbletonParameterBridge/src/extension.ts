/* eslint-disable @typescript-eslint/no-non-null-assertion */
import {
  initialize,
  MidiTrack,
  type ActivationContext,
} from "@ableton-extensions/sdk";
import * as http from "node:http";
import * as urlModule from "node:url";

const HTTP_PORT = 9878;

export function activate(activation: ActivationContext) {
  const api = initialize(activation, "1.0.0");

  let server: http.Server | null = null;

  function startServer() {
    if (server !== null) {
      console.log("[ParameterBridge] Server already running on port " + HTTP_PORT);
      return;
    }
    console.log("[ParameterBridge] Starting HTTP server on port " + HTTP_PORT);

    server = http.createServer(async (req, res) => {
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Access-Control-Allow-Origin", "*");

    try {
      const parsed = urlModule.parse(req.url ?? "/", true);
      const pathname = parsed.pathname ?? "/";
      const q = parsed.query as Record<string, string>;

      // GET /health — liveness check
      if (req.method === "GET" && pathname === "/health") {
        res.writeHead(200);
        res.end(JSON.stringify({ status: "ok", port: HTTP_PORT }));
        return;
      }

      // GET /tracks — list all tracks and their devices with param counts
      if (req.method === "GET" && pathname === "/tracks") {
        const song = api.application.song;
        const result = song.tracks.map((track, i) => ({
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
        const trackIndex = parseInt(q["track"] ?? "0");
        const deviceIndex = parseInt(q["device"] ?? "0");

        const tracks = api.application.song.tracks;
        const track = tracks[trackIndex];
        if (track === undefined) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Track index out of range" }));
          return;
        }
        const device = track.devices[deviceIndex];
        if (device === undefined) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Device index out of range" }));
          return;
        }

        const paramData = await Promise.all(
          device.parameters.map(async (p, i) => {
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
                value_items: [] as string[],
              };
            }
          })
        );

        res.writeHead(200);
        res.end(JSON.stringify({
          track_index: trackIndex,
          track_name: track.name,
          device_index: deviceIndex,
          device_name: device.name,
          parameters: paramData,
        }));
        return;
      }

      // POST /params — set a parameter value
      // Body: { track: 0, device: 0, param_index?: 0, param_name?: "Filter", value: 64 }
      if (req.method === "POST" && pathname === "/params") {
        const body = await readBody(req);
        const { track: trackIndex, device: deviceIndex, param_index, param_name, value } =
          JSON.parse(body) as {
            track: number;
            device: number;
            param_index?: number;
            param_name?: string;
            value: number;
          };

        const track = api.application.song.tracks[trackIndex];
        if (track === undefined) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Track index out of range" }));
          return;
        }
        const device = track.devices[deviceIndex];
        if (device === undefined) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Device index out of range" }));
          return;
        }
        const params = device.parameters;

        let param = null;
        if (param_index !== undefined) {
          param = params[param_index] ?? null;
        } else if (param_name !== undefined) {
          param =
            params.find((p) => p.name.toLowerCase() === param_name.toLowerCase()) ?? null;
        }

        if (param === null) {
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

      // GET /snapshot?track=0&device=0 — capture all parameter values
      if (req.method === "GET" && pathname === "/snapshot") {
        const trackIndex = parseInt(q["track"] ?? "0");
        const deviceIndex = parseInt(q["device"] ?? "0");

        const track = api.application.song.tracks[trackIndex];
        if (track === undefined) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Track index out of range" }));
          return;
        }
        const device = track.devices[deviceIndex];
        if (device === undefined) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Device index out of range" }));
          return;
        }

        const snapshot: Record<string, number> = {};
        for (const p of device.parameters) {
          try {
            snapshot[p.name] = await p.getValue();
          } catch (_) {
            // skip unreadable params
          }
        }
        res.writeHead(200);
        res.end(JSON.stringify({
          track_index: trackIndex,
          device_index: deviceIndex,
          device_name: device.name,
          snapshot,
        }));
        return;
      }

      // POST /snapshot — bulk restore: set all params from a saved snapshot dict
      // Body: { track: 0, device: 0, params: { "Filter Freq": 0.8, "Resonance": 0.3 } }
      if (req.method === "POST" && pathname === "/snapshot") {
        const body = await readBody(req);
        const { track: trackIndex, device: deviceIndex, params: paramValues } =
          JSON.parse(body) as {
            track: number;
            device: number;
            params: Record<string, number>;
          };

        const track = api.application.song.tracks[trackIndex];
        if (track === undefined) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Track index out of range" }));
          return;
        }
        const device = track.devices[deviceIndex];
        if (device === undefined) {
          res.writeHead(400);
          res.end(JSON.stringify({ error: "Device index out of range" }));
          return;
        }
        const liveParams = device.parameters;

        const results: { name: string; value: number; status: string }[] = [];

        await Promise.all(
          Object.entries(paramValues).map(async ([name, value]) => {
            const param = liveParams.find(
              (p) => p.name.toLowerCase() === name.toLowerCase()
            );
            if (param === undefined) {
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
        res.end(JSON.stringify({
          track_index: trackIndex,
          device_index: deviceIndex,
          device_name: device.name,
          applied,
          total: Object.keys(paramValues).length,
          results,
        }));
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
  }

  // Register a command + context menu entry so the user can start the bridge
  // by right-clicking any MIDI track → "Start Parameter Bridge"
  api.commands.registerCommand("parameter-bridge.start", () => {
    startServer();
  });

  void api.ui.registerContextMenuAction(
    "MidiTrack",
    "Start Parameter Bridge",
    "parameter-bridge.start"
  );

  // Also attempt to start immediately — works in extensions-cli run (dev) mode
  // and if Live calls activate() automatically at startup.
  startServer();

  return () => {
    if (server !== null) {
      server.close();
      server = null;
      console.log("[ParameterBridge] HTTP server stopped");
    }
  };
}

function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => (data += String(chunk)));
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}
