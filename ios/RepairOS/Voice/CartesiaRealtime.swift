@preconcurrency import AVFoundation
import Foundation

// Realtime listening straight to Cartesia, after Samik's PR #11 (Voice/Cartesia.swift there): the gateway mints a
// short-lived access token (POST /v1/voice/token; the key never leaves the Mac) and the phone streams the mic to
// Ink-2 with turn detection, `wss://api.cartesia.ai/stt/turns/websocket` (docs.cartesia.ai/api-reference/stt/turns/
// websocket, use-the-api/stt/turns): turn.start, cumulative turn.update, turn.eager_end (maybe done), turn.resume
// (not done after all), turn.end (done). Keyterms "Lea" and "Leah" bias the name. Used after "Hey Lea" and in the
// follow-up window; the wake word itself stays on the phone (nothing streams before it). Any failure falls back to
// Apple's on-device recognizer, which keeps running.

/// A token and what goes with it, from the gateway.
struct RealtimeGrant: Sendable {
    var token: String
    var version: String
    var keyterms: [String]
    var expires: Date
    var voiceID: String = ""
    var ttsModel: String = "sonic-3.6"
    var speed: Double?
    var emotion: String?
}

extension GatewayClient {
    func realtimeGrant() async throws -> RealtimeGrant {
        var req = voiceRequest("POST", "v1/voice/token")
        req.timeoutInterval = 6
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200,
              let o = try JSONSerialization.jsonObject(with: data) as? [String: Any], let token = o["token"] as? String else {
            throw GatewayError.http((resp as? HTTPURLResponse)?.statusCode ?? 0, String(decoding: data, as: UTF8.self))
        }
        let secs = (o["expires_in"] as? NSNumber)?.doubleValue ?? 300
        return RealtimeGrant(token: token, version: o["version"] as? String ?? "2026-08-14",
                             keyterms: o["keyterms"] as? [String] ?? ["Lea", "Leah"], expires: Date().addingTimeInterval(secs),
                             voiceID: o["voice_id"] as? String ?? "", ttsModel: o["tts_model"] as? String ?? "sonic-3.6",
                             speed: (o["speed"] as? NSNumber)?.doubleValue, emotion: o["emotion"] as? String)
    }

    /// Each realtime socket is counted by the gateway's spend guard before it opens (false = paused: don't open).
    func realtimeStream(kind: String) async -> Bool {
        var req = voiceRequest("POST", "v1/voice/stream", query: "kind=\(kind)")
        req.timeoutInterval = 4
        guard let (_, resp) = try? await URLSession.shared.data(for: req) else { return false }
        return (resp as? HTTPURLResponse)?.statusCode == 200
    }
}

/// The grant, reused until a minute before it expires (every mint is spend-checked on the gateway).
@MainActor
enum RealtimeTokens {
    private static var cached: RealtimeGrant?
    private static var pausedUntil = Date.distantPast

    /// The grant when one is ready now (no waiting): a turn uses Sonic's socket only then.
    static var ready: RealtimeGrant? {
        guard let g = cached, g.expires.timeIntervalSinceNow > 60 else { return nil }
        return g
    }

    static func grant(_ gateway: GatewayClient) async -> RealtimeGrant? {
        if let g = cached, g.expires.timeIntervalSinceNow > 60 { return g }
        guard Date() > pausedUntil else { return nil }
        do {
            let g = try await gateway.realtimeGrant()
            cached = g
            return g
        } catch {
            pausedUntil = Date().addingTimeInterval(60)   // unavailable or paused: no retry loop, Apple listens
            WakeListener.diag.log("realtime_token", ["ok": false, "error": "\(error.localizedDescription)"])
            return nil
        }
    }
}

/// One listening turn on Ink-2. Events come back on the main actor.
final class CartesiaTurns: @unchecked Sendable {
    enum Event: Sendable { case start, update(String), eagerEnd(String), resume, end(String), failed(String) }

    private let task: URLSessionWebSocketTask
    private let onEvent: @Sendable (Event) -> Void
    private let lock = NSLock()
    private var closed = false
    let openedAt = Date()

    init(grant: RealtimeGrant, onEvent: @escaping @Sendable (Event) -> Void) {
        var c = URLComponents(string: "wss://api.cartesia.ai/stt/turns/websocket")!
        c.queryItems = [URLQueryItem(name: "model", value: "ink-2"), URLQueryItem(name: "encoding", value: "pcm_s16le"),
                        URLQueryItem(name: "sample_rate", value: "16000"),
                        URLQueryItem(name: "cartesia_version", value: grant.version),
                        URLQueryItem(name: "access_token", value: grant.token)]
            + grant.keyterms.map { URLQueryItem(name: "keyterm", value: $0) }
        task = URLSession.shared.webSocketTask(with: c.url!)
        self.onEvent = onEvent
        task.resume()
        receive()
    }

    /// 16 kHz mono s16le, ~100 ms at a time.
    func send(_ pcm: Data) {
        guard !isClosed else { return }
        task.send(.data(pcm)) { [weak self] error in
            if let error { self?.fail("send: \(error.localizedDescription)") }
        }
    }

    /// Flush what is buffered (the server answers with the last events, then closes).
    func finish() {
        guard !isClosed else { return }
        task.send(.string(#"{"type":"close"}"#)) { _ in }
    }

    func cancel() {
        lock.withLock { closed = true }
        task.cancel(with: .goingAway, reason: nil)
    }

    private var isClosed: Bool { lock.withLock { closed } }

    private func fail(_ why: String) {
        let first = lock.withLock { () -> Bool in defer { closed = true }; return !closed }
        if first { onEvent(.failed(why)) }
    }

    private func receive() {
        task.receive { [weak self] result in
            guard let self, !self.isClosed else { return }
            switch result {
            case .failure(let error):
                self.fail(error.localizedDescription)
            case .success(let message):
                guard case .string(let text) = message,
                      let o = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else { return self.receive() }
                let transcript = o["transcript"] as? String ?? ""
                switch o["type"] as? String {
                case "turn.start": self.onEvent(.start)
                case "turn.update": self.onEvent(.update(transcript))
                case "turn.eager_end": self.onEvent(.eagerEnd(transcript))
                case "turn.resume": self.onEvent(.resume)
                case "turn.end":
                    self.onEvent(.end(transcript))
                    self.cancel()
                    return
                case "error":
                    self.fail("\(o["message"] ?? "error")")
                    return
                default: break
                }
                self.receive()
            }
        }
    }
}

/// The mic audio as 16 kHz mono s16le, in ~100 ms pieces, plus the last ~2 s kept so a turn opened after "Hey Lea"
/// still gets the words said while the socket was connecting. Fed from the audio thread.
final class PCM16Feed: @unchecked Sendable {
    private let lock = NSLock()
    private var converter: AVAudioConverter?
    private var inFormat: AVAudioFormat?
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private var ring = Data()
    private var pending = Data()
    private static let ringBytes = 16000 * 2 * 2      // 2 s
    private static let chunkBytes = 3200               // 100 ms
    /// Where live chunks go while a turn is open.
    private var sink: (@Sendable (Data) -> Void)?

    func feed(_ buffer: AVAudioPCMBuffer) {
        if inFormat != buffer.format {
            inFormat = buffer.format
            converter = AVAudioConverter(from: buffer.format, to: outFormat)
        }
        guard let converter else { return }
        let ratio = 16000 / buffer.format.sampleRate
        let cap = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: cap) else { return }
        var given = false
        var err: NSError?
        converter.convert(to: out, error: &err) { _, status in
            if given { status.pointee = .noDataNow; return nil }
            given = true
            status.pointee = .haveData
            return buffer
        }
        guard err == nil, out.frameLength > 0, let p = out.int16ChannelData else { return }
        let bytes = Data(bytes: p[0], count: Int(out.frameLength) * 2)
        lock.lock()
        ring.append(bytes)
        if ring.count > Self.ringBytes { ring.removeFirst(ring.count - Self.ringBytes) }
        var send: [Data] = []
        if sink != nil {
            pending.append(bytes)
            while pending.count >= Self.chunkBytes {
                send.append(pending.prefix(Self.chunkBytes))
                pending.removeFirst(Self.chunkBytes)
            }
        }
        let s = sink
        lock.unlock()
        for d in send { s?(d) }
    }

    /// Start sending live chunks to `sink`, after the last `backfill` seconds of audio.
    func open(backfill: TimeInterval, sink: @escaping @Sendable (Data) -> Void) {
        lock.lock()
        let keep = min(ring.count, Int(backfill * 32000) / 2 * 2)
        let past = ring.suffix(keep)
        pending = Data()
        self.sink = sink
        lock.unlock()
        var off = past.startIndex
        while off < past.endIndex {
            let end = min(past.endIndex, off + Self.chunkBytes)
            sink(Data(past[off..<end]))
            off = end
        }
    }

    func close() {
        lock.withLock { sink = nil; pending = Data() }
    }
}

/// One spoken answer on Sonic's socket, `wss://api.cartesia.ai/tts/websocket` (docs.cartesia.ai/api-reference/tts/
/// websocket): each sentence goes in as soon as Claude finishes it, as a continuation of one context (one voice
/// flow across sentences), and the audio comes back as base64 s16le chunks, played on the first one.
final class SonicSocket: @unchecked Sendable {
    private let task: URLSessionWebSocketTask
    private let grant: RealtimeGrant
    private let context = UUID().uuidString
    private let lock = NSLock()
    private var closed = false
    var onAudio: (@Sendable (Data) -> Void)?
    var onDone: (@Sendable () -> Void)?
    var onFailed: (@Sendable (String) -> Void)?

    init(grant: RealtimeGrant) {
        self.grant = grant
        var c = URLComponents(string: "wss://api.cartesia.ai/tts/websocket")!
        c.queryItems = [URLQueryItem(name: "cartesia_version", value: grant.version),
                        URLQueryItem(name: "access_token", value: grant.token)]
        task = URLSession.shared.webSocketTask(with: c.url!)
        task.resume()
        receive()
    }

    /// The next sentence (`more: false` on the last; an empty last one just ends the context).
    func say(_ text: String, more: Bool) {
        var body: [String: Any] = ["model_id": grant.ttsModel, "transcript": text, "voice": grant.voiceID, "language": "en",
                                   "output_format": ["container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000],
                                   "context_id": context, "continue": more]
        var gen: [String: Any] = [:]
        if let speed = grant.speed { gen["speed"] = speed }
        if let emotion = grant.emotion { gen["emotion"] = emotion }
        if !gen.isEmpty { body["generation_config"] = gen }
        guard let data = try? JSONSerialization.data(withJSONObject: body) else { return }
        task.send(.string(String(decoding: data, as: UTF8.self))) { [weak self] error in
            if let error { self?.fail(error.localizedDescription) }
        }
    }

    func cancel() {
        lock.withLock { closed = true }
        task.cancel(with: .goingAway, reason: nil)
    }

    private var isClosed: Bool { lock.withLock { closed } }

    private func fail(_ why: String) {
        let first = lock.withLock { () -> Bool in defer { closed = true }; return !closed }
        if first { onFailed?(why) }
    }

    private func receive() {
        task.receive { [weak self] result in
            guard let self, !self.isClosed else { return }
            switch result {
            case .failure(let error): self.fail(error.localizedDescription)
            case .success(let message):
                guard case .string(let text) = message,
                      let o = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] else { return self.receive() }
                switch o["type"] as? String {
                case "chunk":
                    if let b = o["data"] as? String, let d = Data(base64Encoded: b), !d.isEmpty { self.onAudio?(d) }
                    if o["done"] as? Bool == true { self.finished(); return }
                case "done": self.finished(); return
                case "error": self.fail("\(o["message"] ?? o["error"] ?? "error")"); return
                default: break
                }
                self.receive()
            }
        }
    }

    private func finished() {
        lock.withLock { closed = true }
        onDone?()
        task.cancel(with: .normalClosure, reason: nil)
    }
}
