@preconcurrency import AVFoundation
import Foundation

/// Plays 16-bit mono PCM (the gateway's voice format) as it arrives: each chunk is scheduled on an
/// AVAudioPlayerNode, so speech starts on the first chunk of a stream. Output only; the engine never touches the
/// microphone input node, and all engine work runs on one serial queue, off the main thread (the scan's 60 fps
/// render loop never waits on audio).
final class PCMPlayer: @unchecked Sendable {
    private let queue = DispatchQueue(label: "lea.voice.player", qos: .userInitiated)
    private let engine = AVAudioEngine()
    private let node = AVAudioPlayerNode()
    private let format: AVAudioFormat
    private var attached = false
    /// The utterance being played; chunks and completions of an older one are ignored.
    private var current = 0
    private var carry = Data()
    private var idleStop: DispatchWorkItem?

    init(sampleRate: Double) {
        format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)!
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) {
            [weak self] _ in self?.queue.async { self?.restartAfterRouteChange() }
        }
    }

    /// Starts a new utterance (stopping any other). Returns its id for `append` / `finish`.
    func begin(_ id: Int) {
        queue.async { [self] in
            current = id
            carry.removeAll(keepingCapacity: true)
            idleStop?.cancel()
            if node.isPlaying { node.stop() }
            startEngine()
        }
    }

    /// Schedules PCM bytes (s16le). `started` runs once, on the first chunk that reaches the player.
    func append(_ bytes: Data, id: Int, started: (@Sendable () -> Void)? = nil) {
        queue.async { [self] in
            guard id == current else { return }
            // A route or session change stops the engine mid-utterance: start it again and keep going.
            if !engine.isRunning { startEngine() }
            guard engine.isRunning else { return }
            carry.append(bytes)
            let whole = carry.count - carry.count % 2
            guard whole > 0, let buf = Self.buffer(carry.prefix(whole), format: format) else { return }
            carry = Data(carry.dropFirst(whole))
            node.scheduleBuffer(buf, completionHandler: nil)
            if !node.isPlaying {
                node.play()
                started?()
            }
        }
    }

    /// No more chunks: `done` runs when the last sample has been played (or at once if nothing played).
    func finish(id: Int, done: @escaping @Sendable () -> Void) {
        queue.async { [self] in
            guard id == current, engine.isRunning, node.isPlaying else { done(); return }
            let tail = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 240)!
            tail.frameLength = 240   // 10 ms of silence marks the end
            node.scheduleBuffer(tail, completionCallbackType: .dataPlayedBack) { [weak self] _ in
                self?.queue.async {
                    guard let self, id == self.current else { return }
                    self.scheduleIdleStop()
                    done()
                }
            }
        }
    }

    func stop() {
        queue.async { [self] in
            current += 1
            if node.isPlaying { node.stop() }
            scheduleIdleStop()
        }
    }

    // MARK: -

    private func startEngine() {
        if !attached {
            engine.attach(node)
            engine.connect(node, to: engine.mainMixerNode, format: format)
            attached = true
        }
        if !engine.isRunning {
            do { try engine.start() } catch { log.error("voice engine: \(error.localizedDescription, privacy: .public)") }
        }
    }

    /// Leave the engine idle a while (cues come every few seconds), then stop it so it costs nothing.
    private func scheduleIdleStop() {
        idleStop?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.node.isPlaying else { return }
            self.engine.stop()
        }
        idleStop = work
        queue.asyncAfter(deadline: .now() + 15, execute: work)
    }

    private func restartAfterRouteChange() {
        // The engine stops itself on a route or session change; a later begin() starts it again.
        if !engine.isRunning { node.stop() }
    }

    static func buffer(_ pcm: Data, format: AVAudioFormat) -> AVAudioPCMBuffer? {
        let frames = pcm.count / 2
        guard frames > 0, let buf = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else { return nil }
        buf.frameLength = AVAudioFrameCount(frames)
        let out = buf.floatChannelData![0]
        pcm.withUnsafeBytes { raw in
            let src = raw.bindMemory(to: Int16.self)
            for i in 0..<frames { out[i] = Float(Int16(littleEndian: src[i])) / 32768 }
        }
        return buf
    }

    /// The samples of a 16-bit mono WAV file (the gateway's inline answer audio), or nil.
    static func pcm(fromWAV data: Data) -> Data? {
        guard data.count > 44, data.prefix(4) == Data("RIFF".utf8) else { return nil }
        var i = 12
        while i + 8 <= data.count {
            let id = data.subdata(in: i..<i + 4)
            let size = data.subdata(in: i + 4..<i + 8).withUnsafeBytes { Int(UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self))) }
            if id == Data("data".utf8) { return data.subdata(in: i + 8..<min(data.count, i + 8 + size)) }
            i += 8 + size + size % 2
        }
        return nil
    }
}
