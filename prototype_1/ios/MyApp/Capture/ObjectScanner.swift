import Combine
import Foundation
import ModelIO
import RealityKit
import SwiftUI   // ObjectCaptureSession lives in the RealityKit + SwiftUI overlay

/// "Make a render of this": Apple's guided Object Capture on iPhones with LiDAR (ObjectCaptureSession.isSupported),
/// then an on-device model (PhotogrammetrySession; iOS builds `.reduced` detail, USDZ only). The USDZ is the visual
/// model; an OBJ copy made with ModelIO is what the bridge's Open3D QA measures. The captured stills are kept as
/// evidence. iPhones without LiDAR use guided photo capture and the Mac builds the model instead.
@MainActor
final class ObjectScanner: ObservableObject {
    enum Phase: Equatable {
        case idle, preparing, detecting, capturing, passDone, finishing
        case reconstructing(Double)
        case done
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var shots = 0
    @Published private(set) var feedback: String?
    @Published private(set) var session: ObjectCaptureSession?
    /// The stills always; the model only when building it worked.
    private(set) var result: (images: URL, usdz: URL?, obj: URL?)?

    static var supported: Bool { ObjectCaptureSession.isSupported }

    private var work: URL?
    private var watchers: [Task<Void, Never>] = []

    func start() {
        cancel()
        let work = FileManager.default.temporaryDirectory.appendingPathComponent("LeaScan-\(UUID().uuidString)", isDirectory: true)
        let images = work.appendingPathComponent("images", isDirectory: true)
        let checkpoint = work.appendingPathComponent("checkpoint", isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: images, withIntermediateDirectories: true)
            try FileManager.default.createDirectory(at: checkpoint, withIntermediateDirectories: true)
        } catch {
            phase = .failed("Could not prepare scan storage: \(error.localizedDescription)")
            return
        }
        self.work = work
        let session = ObjectCaptureSession()
        var configuration = ObjectCaptureSession.Configuration()
        configuration.checkpointDirectory = checkpoint
        session.start(imagesDirectory: images, configuration: configuration)
        self.session = session
        phase = .preparing
        watch(session, images: images, checkpoint: checkpoint)
    }

    /// Box the object, then walk around it. Each call advances one step of Apple's capture flow.
    func advance() {
        guard let session else { return }
        switch phase {
        case .preparing, .idle: if session.startDetecting() { phase = .detecting }
        case .detecting: session.startCapturing()
        case .passDone: session.beginNewScanPass(); phase = .capturing
        default: break
        }
    }

    /// After a pass: flip the object to capture its underside (needs a flippable object), or finish.
    func flip() { session?.beginNewScanPassAfterFlip(); phase = .capturing }
    func finish() { session?.finish(); phase = .finishing }

    /// Discards this scan, its stills included: only on an explicit cancel or discard.
    func cancel() {
        watchers.forEach { $0.cancel() }
        watchers = []
        session?.cancel()
        session = nil
        if let work { try? FileManager.default.removeItem(at: work) }
        work = nil
        result = nil
        phase = .idle
        shots = 0
        feedback = nil
    }

    /// Stops capturing but keeps the stills taken so far for the caller to save.
    func stopKeepingPhotos() -> URL? {
        guard let work else { return nil }
        watchers.forEach { $0.cancel() }
        watchers = []
        session?.cancel()
        session = nil
        self.work = nil          // so a later cancel() cannot delete them
        phase = .idle
        return work.appendingPathComponent("images", isDirectory: true)
    }

    /// Hands the finished files to the caller (who moves them into the session) and forgets them here.
    func takeResult() -> (images: URL, usdz: URL?, obj: URL?)? {
        defer { watchers.forEach { $0.cancel() }; watchers = []; result = nil; phase = .idle; session = nil; work = nil }
        return result
    }

    private func watch(_ session: ObjectCaptureSession, images: URL, checkpoint: URL) {
        watchers.append(Task { [weak self] in
            for await state in session.stateUpdates {
                guard let self else { return }
                switch state {
                case .capturing: if self.phase != .passDone { self.phase = .capturing }
                case .completed: await self.reconstruct(images: images, checkpoint: checkpoint)
                case .failed(let error): self.phase = .failed(error.localizedDescription)
                default: break
                }
            }
        })
        watchers.append(Task { [weak self] in
            for await done in session.userCompletedScanPassUpdates where done { self?.phase = .passDone }
        })
        watchers.append(Task { [weak self] in
            for await n in session.numberOfShotsTakenUpdates { self?.shots = n }
        })
        watchers.append(Task { [weak self] in
            for await set in session.feedbackUpdates { self?.feedback = Self.describe(set) }
        })
    }

    private func reconstruct(images: URL, checkpoint: URL) async {
        session = nil
        guard let work else { return }
        guard PhotogrammetrySession.isSupported else {
            result = (images, nil, nil)
            phase = .failed("This iPhone can't build models on-device. Save the photos and the Mac will build it.")
            return
        }
        let usdz = work.appendingPathComponent("model.usdz")
        phase = .reconstructing(0)
        do {
            let photogrammetry = try PhotogrammetrySession(input: images, configuration: .init(checkpointDirectory: checkpoint))
            try photogrammetry.process(requests: [.modelFile(url: usdz)])
            for try await output in photogrammetry.outputs {
                switch output {
                case .requestProgress(_, let fraction): phase = .reconstructing(fraction)
                case .requestError(_, let error): throw error
                case .processingComplete:
                    let obj = work.appendingPathComponent("model.obj")
                    try Self.exportOBJ(usdz, to: obj)
                    result = (images, usdz, obj)
                    phase = .done
                    return
                case .processingCancelled: phase = .failed("Model building was cancelled."); return
                default: break
                }
            }
        } catch {
            result = (images, nil, nil)
            phase = .failed("Model building failed: \(error.localizedDescription). Save the photos and the Mac can build it instead.")
        }
    }

    /// Open3D can't read USDZ; ModelIO writes the same geometry as OBJ (USD units, metres for Object Capture).
    nonisolated static func exportOBJ(_ usdz: URL, to obj: URL) throws {
        let asset = MDLAsset(url: usdz)
        guard asset.count > 0, MDLAsset.canExportFileExtension("obj") else {
            throw NSError(domain: "Lea", code: 3, userInfo: [NSLocalizedDescriptionKey: "The model could not be converted for measurement."])
        }
        try asset.export(to: obj)
    }

    private static func describe(_ set: Set<ObjectCaptureSession.Feedback>) -> String? {
        if set.contains(.objectNotDetected) { return "I can't find the object. Center it in the box." }
        if set.contains(.objectTooClose) { return "Move back a little." }
        if set.contains(.objectTooFar) { return "Move closer." }
        if set.contains(.movingTooFast) { return "Slow down." }
        if set.contains(.environmentTooDark) || set.contains(.environmentLowLight) { return "It's too dark. Add light." }
        if set.contains(.outOfFieldOfView) { return "Keep the object in view." }
        return nil
    }
}
