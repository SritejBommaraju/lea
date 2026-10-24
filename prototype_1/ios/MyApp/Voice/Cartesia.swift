import Foundation

// Cartesia realtime APIs, authenticated with the short-lived access token the bridge mints.
// STT: wss://api.cartesia.ai/stt/turns/websocket (Ink 2; turn.update is cumulative, turn.end is final).
// TTS: wss://api.cartesia.ai/tts/websocket (Sonic; base64 audio in "chunk" messages until "done").

nonisolated final class CartesiaSTT: @unchecked Sendable {
    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onError: ((String) -> Void)?

    private let grant: CartesiaGrant
    private var task: URLSessionWebSocketTask?
    private var finished = false

    init(grant: CartesiaGrant) { self.grant = grant }

    func connect() throws {
        var components = URLComponents(string: "wss://api.cartesia.ai/stt/turns/websocket")!
        components.queryItems = [
            URLQueryItem(name: "model", value: "ink-2"),
            URLQueryItem(name: "encoding", value: "pcm_s16le"),
            URLQueryItem(name: "sample_rate", value: "16000"),
            URLQueryItem(name: "cartesia_version", value: grant.version),
            URLQueryItem(name: "access_token", value: grant.token),
            URLQueryItem(name: "keyterm", value: "Leah"),
            URLQueryItem(name: "keyterm", value: "Grain"),
        ]
        let task = URLSession.shared.webSocketTask(with: components.url!)
        self.task = task
        task.resume()
        receive()
    }

    func send(_ pcm: Data) {
        task?.send(.data(pcm)) { [weak self] error in if let error { self?.onError?("Speech stream failed: \(error.localizedDescription)") } }
    }

    /// Ends the utterance: Cartesia processes buffered audio and replies with turn.end.
    func finish() {
        task?.send(.string(#"{"type":"close"}"#)) { _ in }
    }

    func cancel() {
        finished = true
        task?.cancel(with: .goingAway, reason: nil)
    }

    private func receive() {
        task?.receive { [weak self] result in
            guard let self, !self.finished else { return }
            switch result {
            case .failure(let error):
                self.finished = true
                self.onError?("Speech recognition disconnected: \(error.localizedDescription)")
            case .success(let message):
                if case .string(let text) = message, let data = text.data(using: .utf8),
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    switch json["type"] as? String {
                    case "turn.update": self.onPartial?(json["transcript"] as? String ?? "")
                    case "turn.end":
                        self.finished = true
                        self.onFinal?(json["transcript"] as? String ?? "")
                        self.task?.cancel(with: .normalClosure, reason: nil)
                        return
                    case "error":
                        self.finished = true
                        self.onError?("Cartesia: \(json["message"] as? String ?? "speech error")")
                        return
                    default: break
                    }
                }
                self.receive()
            }
        }
    }
}

nonisolated struct CartesiaTTS: Sendable {
    let grant: CartesiaGrant

    /// Streams float32 mono samples at 24 kHz as they are generated.
    func stream(_ transcript: String) -> AsyncThrowingStream<[Float], Error> {
        AsyncThrowingStream { continuation in
            var components = URLComponents(string: "wss://api.cartesia.ai/tts/websocket")!
            components.queryItems = [
                URLQueryItem(name: "cartesia_version", value: grant.version),
                URLQueryItem(name: "access_token", value: grant.token),
            ]
            let task = URLSession.shared.webSocketTask(with: components.url!)
            task.resume()
            let request: [String: Any] = [
                "model_id": "sonic-3.6", "transcript": transcript, "voice": grant.voiceId,
                "output_format": ["container": "raw", "encoding": "pcm_f32le", "sample_rate": 24_000],
                "context_id": UUID().uuidString, "continue": false, "language": "en",
            ]
            let body = String(data: try! JSONSerialization.data(withJSONObject: request), encoding: .utf8)!
            task.send(.string(body)) { error in if let error { continuation.finish(throwing: error) } }

            @Sendable func next() {
                task.receive { result in
                    switch result {
                    case .failure(let error): continuation.finish(throwing: error)
                    case .success(.string(let text)):
                        guard let json = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] else { return next() }
                        switch json["type"] as? String {
                        case "chunk":
                            if let b64 = json["data"] as? String, let audio = Data(base64Encoded: b64) {
                                continuation.yield(audio.withUnsafeBytes { Array($0.bindMemory(to: Float.self)) })
                            }
                            if json["done"] as? Bool == true { continuation.finish(); task.cancel(with: .normalClosure, reason: nil) } else { next() }
                        case "done":
                            continuation.finish()
                            task.cancel(with: .normalClosure, reason: nil)
                        case "error":
                            continuation.finish(throwing: NSError(domain: "Cartesia", code: json["status_code"] as? Int ?? 0,
                                                                  userInfo: [NSLocalizedDescriptionKey: json["message"] as? String ?? "speech error"]))
                        default: next()
                        }
                    case .success: next()
                    }
                }
            }
            next()
            continuation.onTermination = { _ in task.cancel(with: .goingAway, reason: nil) }
        }
    }
}
