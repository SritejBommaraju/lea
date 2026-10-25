import Foundation
import simd

/// The gateway's voice routes (backend/repairos/voice/routes.py). The phone sends only its device token; the
/// Cartesia key stays on the backend.
struct VoiceStatus: Sendable, Equatable {
    var cartesia: Bool
    var answers: Bool
    var sampleRate: Double
    var voiceID: String?
}

struct VoiceAnswer: Sendable {
    var transcript: String
    var answerText: String
    /// 16-bit mono WAV (inline audio), when the gateway made it.
    var audio: Data?
    var timings: [String: Int]
}

extension GatewayClient {
    private static let voiceSession: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 15
        c.timeoutIntervalForResource = 60
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    func voiceRequest(_ method: String, _ path: String, query: String? = nil) -> URLRequest {
        var comps = URLComponents(url: baseURL, resolvingAgainstBaseURL: false)!
        comps.path = (comps.path.hasSuffix("/") ? String(comps.path.dropLast()) : comps.path) + "/" + path
        comps.percentEncodedQuery = query
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        return req
    }

    private func voiceSend(_ req: URLRequest) async throws -> Data {
        let (data, resp) = try await Self.voiceSession.data(for: req)
        guard let http = resp as? HTTPURLResponse else { throw GatewayError.message("No HTTP answer.") }
        guard http.statusCode == 200 else { throw GatewayError.http(http.statusCode, String(decoding: data, as: UTF8.self)) }
        return data
    }

    func voiceStatus() async throws -> VoiceStatus {
        var req = voiceRequest("GET", "v1/voice/status")
        req.timeoutInterval = 5
        let o = try JSONSerialization.jsonObject(with: try await voiceSend(req)) as? [String: Any] ?? [:]
        return VoiceStatus(cartesia: o["cartesia"] as? Bool ?? false, answers: o["answers"] as? Bool ?? false,
                           sampleRate: (o["sample_rate"] as? NSNumber)?.doubleValue ?? 24000,
                           voiceID: o["voice_id"] as? String)
    }

    /// POST /v1/voice/tts {text, format: pcm}: raw s16le samples, streamed while they are made.
    func ttsRequest(_ text: String) -> URLRequest {
        var req = voiceRequest("POST", "v1/voice/tts")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text, "format": "pcm"])
        req.timeoutInterval = 10
        return req
    }

    func ttsStream(_ text: String) async throws -> URLSession.AsyncBytes {
        let (bytes, resp) = try await Self.voiceSession.bytes(for: ttsRequest(text))
        guard let http = resp as? HTTPURLResponse else { throw GatewayError.message("No HTTP answer.") }
        guard http.statusCode == 200 else { throw GatewayError.http(http.statusCode, "") }
        return bytes
    }

    func ttsPCM(_ text: String) async throws -> Data {
        try await voiceSend(ttsRequest(text))
    }

    /// POST /v1/voice/stt with the recording (m4a): the words.
    func stt(audio: Data, contentType: String = "audio/m4a") async throws -> String {
        var req = voiceRequest("POST", "v1/voice/stt")
        req.setValue(contentType, forHTTPHeaderField: "Content-Type")
        req.httpBody = audio
        let o = try JSONSerialization.jsonObject(with: try await voiceSend(req)) as? [String: Any] ?? [:]
        return (o["transcript"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// POST /v1/voice/ask?scan_id=: the recording (or the phone's own transcript as `question`) → the answer.
    /// `inlineAudio: false` returns the texts at once; the caller streams the answer's speech.
    func ask(scanID: String, audio: Data?, question: String?, inlineAudio: Bool) async throws -> VoiceAnswer {
        let q = "scan_id=\(scanID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? scanID)"
            + (inlineAudio ? "" : "&audio=none")
        var req = voiceRequest("POST", "v1/voice/ask", query: q)
        req.timeoutInterval = 20
        if let audio {
            req.setValue("audio/m4a", forHTTPHeaderField: "Content-Type")
            req.httpBody = audio
        } else {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: ["scan_id": scanID, "question": question ?? ""])
        }
        let o = try JSONSerialization.jsonObject(with: try await voiceSend(req)) as? [String: Any] ?? [:]
        return VoiceAnswer(transcript: o["transcript"] as? String ?? "", answerText: o["answer_text"] as? String ?? "",
                           audio: (o["audio"] as? String).flatMap { Data(base64Encoded: $0) },
                           timings: o["timings_ms"] as? [String: Int] ?? [:])
    }
}

// MARK: - Conversation

/// One event of a conversation turn (POST /v1/voice/converse): JSON events and PCM audio, in order.
enum ConverseEvent: Sendable {
    case transcript(String)
    case sentence(String, audio: Bool)
    case audio(Data)
    case audioOff
    case action(String, label: String)
    /// Lea points at something in the turn's photo (fractions of it, 0,0 top left).
    case mark(LeaMark)
    /// Lea asks the phone to measure between two points of the turn's last photo (fractions of it), with LiDAR.
    case measure(CGPoint, CGPoint)
    /// On a report: Lea moves its 3D model (show a part, look from a side, zoom), right after the sentence it goes with.
    case stage(StageCommand)
    /// The real photo of the spot she just showed (a blob sha for GET /v1/blobs, ringed where it is), for `forID`
    /// (the finding or spot id of a `show` / `point`).
    case photo(forID: String, crop: String)
    /// `heldID`: a held answer's id, for `commitHeld` once the person's final words confirm it.
    case done(answer: String, timings: [String: Int], heldID: String? = nil)
}

extension GatewayClient {
    /// `held`: an answer started before the person finished (Ink-2's eager end); the gateway keeps it out of the
    /// conversation until `commitHeld`.
    func converseRequest(scanID: String?, audio: Data?, text: String?, speech: Bool, held: Bool = false,
                         commit: String? = nil, live: [String: String] = [:], sessionID: String? = nil,
                         images: [String] = []) -> URLRequest {
        var q = "audio=\(speech ? "stream" : "none")"
        if held { q += "&held=1" }
        if let commit { q += "&commit=\(commit)" }
        if let scanID { q += "&scan_id=\(scanID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? scanID)" }
        // Before the scan exists, the session tells the gateway what the label photo identified (the brief).
        if let sessionID { q += "&session_id=\(sessionID.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? sessionID)" }
        // What the camera showed (blobs uploaded while the person talked, oldest first, the last one now): Lea sees
        // them and can point at things in the last one.
        for image in images.suffix(3) { q += "&image=\(image.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? image)" }
        // What the screen shows now (a scan's stage, cue, progress): for this turn only.
        if !live.isEmpty, let json = try? JSONSerialization.data(withJSONObject: live, options: .sortedKeys),
           let encoded = String(decoding: json, as: UTF8.self).addingPercentEncoding(withAllowedCharacters: .alphanumerics) {
            q += "&live=\(encoded)"
        }
        var req = voiceRequest("POST", "v1/voice/converse", query: q)
        req.timeoutInterval = 25
        if let audio {
            req.setValue("audio/m4a", forHTTPHeaderField: "Content-Type")
            req.httpBody = audio
        } else {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["text": text ?? ""])
        }
        return req
    }

    /// The turn's frames as they arrive (1 byte kind, 4 bytes big-endian length, payload).
    func converse(scanID: String?, audio: Data?, text: String?, speech: Bool, held: Bool = false,
                  commit: String? = nil, live: [String: String] = [:], sessionID: String? = nil,
                  images: [String] = []) -> AsyncThrowingStream<ConverseEvent, Error> {
        let req = converseRequest(scanID: scanID, audio: audio, text: text, speech: speech, held: held, commit: commit,
                                  live: live, sessionID: sessionID, images: images)
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, resp) = try await URLSession.shared.bytes(for: req)
                    guard let http = resp as? HTTPURLResponse else { throw GatewayError.message("No HTTP answer.") }
                    guard http.statusCode == 200 else { throw GatewayError.http(http.statusCode, "") }
                    var header = Data(), payload = Data(), need = 0, kind: UInt8 = 0
                    for try await b in bytes {
                        if need == 0 {
                            header.append(b)
                            if header.count == 5 {
                                kind = header[header.startIndex]
                                need = header.suffix(4).reduce(0) { $0 << 8 | Int($1) }
                                header.removeAll(keepingCapacity: true)
                                payload.removeAll(keepingCapacity: true)
                                if need == 0, let e = Self.event(kind, Data()) { continuation.yield(e) }
                            }
                            continue
                        }
                        payload.append(b)
                        // Audio goes out in 50 ms pieces even inside a long frame, so playback starts early.
                        if kind == UInt8(ascii: "A"), payload.count >= 2400, payload.count < need {
                            continuation.yield(.audio(payload))
                            need -= payload.count
                            payload.removeAll(keepingCapacity: true)
                            continue
                        }
                        if payload.count == need {
                            if let e = Self.event(kind, payload) { continuation.yield(e) }
                            need = 0
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    static func event(_ kind: UInt8, _ payload: Data) -> ConverseEvent? {
        if kind == UInt8(ascii: "A") { return payload.isEmpty ? nil : .audio(payload) }
        guard let o = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] else { return nil }
        switch o["type"] as? String {
        case "transcript": return .transcript(o["text"] as? String ?? "")
        case "sentence": return .sentence(o["text"] as? String ?? "", audio: o["audio"] as? Bool ?? false)
        case "audio_off": return .audioOff
        case "action": return .action(o["action"] as? String ?? "", label: o["label"] as? String ?? "")
        case "mark":
            guard let x = o["x"] as? Double, let y = o["y"] as? Double else { return nil }
            return .mark(LeaMark(x: x, y: y, label: o["label"] as? String ?? ""))
        case "measure":
            guard let x1 = o["x1"] as? Double, let y1 = o["y1"] as? Double, let x2 = o["x2"] as? Double,
                  let y2 = o["y2"] as? Double else { return nil }
            return .measure(CGPoint(x: x1, y: y1), CGPoint(x: x2, y: y2))
        case "done": return .done(answer: o["answer_text"] as? String ?? "", timings: o["timings_ms"] as? [String: Int] ?? [:],
                                  heldID: o["held_id"] as? String)
        case "show", "point", "view", "zoom": return Self.stage(o).map(ConverseEvent.stage)
        case "photo":
            guard let id = o["for"] as? String, let sha = o["crop"] as? String, !sha.isEmpty else { return nil }
            return .photo(forID: id, crop: sha)
        default: return nil
        }
    }

    /// A 3D-model move from its J event; nil when malformed (it is then ignored, never guessed).
    static func stage(_ o: [String: Any]) -> StageCommand? {
        func num(_ k: String) -> Float? { (o[k] as? NSNumber).map(\.floatValue) }
        switch o["type"] as? String {
        case "show": return (o["part"] as? String).flatMap { $0.isEmpty ? nil : StageCommand.show($0) }
        case "point":
            guard let id = o["id"] as? String, let x = num("x"), let y = num("y"), let z = num("z") else { return nil }
            return .point(id: id, at: SIMD3(x, y, z), label: o["label"] as? String ?? "", radius: num("radius") ?? 0.015)
        case "view": return (o["side"] as? String).flatMap(StageSide.init(rawValue:)).map(StageCommand.view)
        case "zoom":
            switch o["dir"] as? String {
            case "in": return .zoom(in: true)
            case "out": return .zoom(in: false)
            default: return nil
            }
        default: return nil
        }
    }

    /// POST /v1/voice/conversation/commit: a held answer the person's final words confirmed joins the conversation.
    func commitHeld(scanID: String?, heldID: String) async throws {
        let q = scanID.map { "scan_id=\($0.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? $0)" }
        var req = voiceRequest("POST", "v1/voice/conversation/commit", query: q)
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["held_id": heldID])
        req.timeoutInterval = 3   // the next turn waits for it: never long
        _ = try await voiceSend(req)
    }

    func conversation(scanID: String?) async throws -> [(role: String, text: String)] {
        let q = scanID.map { "scan_id=\($0.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? $0)" }
        let req = voiceRequest("GET", "v1/voice/conversation", query: q)
        let (data, resp) = try await URLSession.shared.data(for: req)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return [] }
        let turns = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["turns"] as? [[String: Any]] ?? []
        return turns.compactMap { t in (t["text"] as? String).map { (t["role"] as? String ?? "", $0) } }
    }
}
