import Foundation

// Wire types for the Lea bridge (/v1). Field names match the SpacetimeDB module rows the bridge
// serves; timestamps arrive as ISO-8601 strings and 64-bit counters as numbers.

nonisolated struct RemoteSession: Codable, Identifiable, Sendable {
    let id: String
    let title: String
    let grainProjectId: String
    let referenceMeshAssetId: String
    let activeDesignVersionId: String
    let createdAt: Date
    let updatedAt: Date
}

nonisolated struct RemoteAsset: Codable, Identifiable, Sendable {
    let id: String
    let sessionId: String
    let kind: String
    let sha256: String
    let bytes: Int
    let mediaType: String
    let sourceAssetIds: [String]
    let metaJson: String
    let createdAt: Date
}

nonisolated struct QAPolicy: Codable, Identifiable, Sendable {
    let id: String
    let scaleAnchorLabel: String
    let scaleAnchorMm: Double
    let holdoutLabel: String
    let holdoutMm: Double
    let maxHoldoutErrorMm: Double
    let minCoverage: Double
    let maxFitRmsMm: Double
    let minFitInlierShare: Double
}

nonisolated struct RemoteJob: Codable, Identifiable, Sendable {
    let id: String
    let kind: String
    let status: String
    let attempt: Int
    let progress: String
    let resultJson: String
    let error: String
    let createdAt: Date
    let updatedAt: Date

    var isTerminal: Bool { ["succeeded", "failed", "needs_user", "uncertain"].contains(status) }
}

nonisolated struct MeshReport: Codable, Identifiable, Sendable {
    let id: String
    let jobId: String
    let meshAssetId: String
    let reportAssetId: String
    let referenceMeshAssetId: String
    let engine: String
    let scaleFactor: Double
    let measuredHoldoutMm: Double
    let holdoutErrorMm: Double
    let coverage: Double
    let fitRmsMm: Double
    let fitInlierShare: Double
    let verdict: String
    let failedChecks: [String]
    let createdAt: Date

    var passed: Bool { verdict == "pass" }
}

nonisolated struct Finding: Codable, Identifiable, Sendable {
    let id: String
    let subject: String
    let problem: String
    let source: String
    let state: String
    let evidenceAssetIds: [String]
    let referenceAssetId: String
    let meshReportId: String
    let confidenceKind: String
    let confidenceText: String
    let detail: String
    let createdAt: Date
}

nonisolated struct RepairCheck: Codable, Identifiable, Sendable {
    let id: String
    let findingId: String
    let purpose: String
    let instruction: String
    let safety: String
    let powerState: String
    let expectedMin: Double?
    let expectedMax: Double?
    let expectedUnit: String
    let expectedSource: String
    let outcome: String
    let observationAssetId: String
    let readingValue: Double?
    let readingUnit: String
    let note: String
}

nonisolated struct RepairAttempt: Codable, Identifiable, Sendable {
    let id: String
    let findingId: String
    let action: String
    let verifyCheckId: String
    let result: String
}

nonisolated struct VoiceTurn: Codable, Identifiable, Sendable {
    let id: String
    let mode: String
    let transcript: String
    let replyText: String
    let commandId: String
    let createdAt: Date
}

nonisolated struct DesignCommand: Codable, Identifiable, Sendable {
    let id: String
    let origin: String
    let text: String
    let meshReportId: String
    let jobId: String
    let grainProjectId: String
    let preVersionId: String
    let postVersionId: String
    let status: String
    let createdAt: Date
}

nonisolated struct DesignVersion: Codable, Identifiable, Sendable {
    let id: String
    let commandId: String
    let phase: String
    let manifestAssetId: String
    let fileCount: Int
    let buildOk: Bool?
    let createdAt: Date
}

nonisolated struct SessionSnapshot: Codable, Sendable {
    let session: RemoteSession
    let assets: [RemoteAsset]
    let policies: [QAPolicy]
    let jobs: [RemoteJob]
    let meshReports: [MeshReport]
    let findings: [Finding]
    let checks: [RepairCheck]
    let repairs: [RepairAttempt]
    let voiceTurns: [VoiceTurn]
    let designCommands: [DesignCommand]
    let designVersions: [DesignVersion]
    let lastSeq: Int
}

nonisolated struct BridgeHealth: Codable, Sendable {
    struct Store: Codable, Sendable { let connected: Bool; let database: String }
    struct Grain: Codable, Sendable { let url: String; let reachable: Bool; let agentReady: Bool?; let error: String? }
    struct Voice: Codable, Sendable { let cartesiaConfigured: Bool }
    struct Vision: Codable, Sendable { let anthropicConfigured: Bool; let jevConfigured: Bool? }
    let ok: Bool
    let spacetimedb: Store
    let grain: Grain
    let voice: Voice
    let vision: Vision
}

nonisolated struct GrainLive: Codable, Sendable {
    struct Project: Codable, Sendable {
        struct Build: Codable, Sendable { let ok: Bool; let errors: Int; let seconds: Double? }
        let id: String
        let name: String
        let busy: Bool
        let waiting: Bool
        let turns: Int
        let last_build: Build?
    }
    struct Question: Codable, Sendable {
        struct Item: Codable, Sendable { let text: String; let options: [String]? }
        let why: String?
        let questions: [Item]
    }
    struct Message: Codable, Sendable {
        let role: String?
        let type: String?
        let text: String?
        let name: String?
    }
    struct File: Codable, Sendable { let path: String; let size: Int }
    let linked: Bool
    let project: Project?
    let question: Question?
    let messages: [Message]?
    let files: [File]?
}

nonisolated struct DesignSnapshotManifest: Codable, Sendable {
    struct Entry: Codable, Sendable, Identifiable {
        let path: String
        let size: Int
        let sha256: String
        let assetId: String
        var id: String { path }
    }
    struct StepCheck: Codable, Sendable {
        struct Dimension: Codable, Sendable, Hashable { let dimension: String; let expected_mm: Double; let tolerance_mm: Double; let measured_mm: Double; let ok: Bool }
        let ok: Bool
        let solids: Int?
        let valid: Bool?
        let bbox_mm: [Double]?
        let dimension_checks: [Dimension]?
        let error: String?
    }
    let grainProjectId: String
    let phase: String
    let takenAt: String
    let files: [Entry]
    let stepCheck: StepCheck?
}

/// The validated scan for the 3D view (bridge mesh-reports/:id/view): float32 xyz in mm and uint32 triangles, base64.
nonisolated struct MeshView: Codable, Sendable {
    struct Region: Codable, Sendable { let findingId: String; let kind: String; let centroid: [Double]; let extent: [Double] }
    let units: String
    let verdict: String
    let vertices: Int
    let triangles: Int
    let positions: String
    let indices: String
    let regions: [Region]
}

nonisolated struct GrainNets: Codable, Sendable {
    struct Net: Codable, Sendable, Hashable { let net: String; let min: Double; let max: Double; let source: String }
    let nets: [Net]
}

nonisolated struct CartesiaGrant: Codable, Sendable {
    let token: String
    let expiresInSeconds: Int
    let version: String
    let voiceId: String
}

nonisolated struct SessionEvent: Codable, Sendable {
    let seq: Int
    let kind: String
    let entityId: String
}

nonisolated struct BridgeErrorBody: Codable, Sendable {
    struct Inner: Codable, Sendable { let status: Int; let message: String }
    let error: Inner
}

/// Jev's fast look at a live frame (bridge /look): guidance, never evidence.
nonisolated struct LookResult: Codable, Sendable, Equatable {
    struct Box: Codable, Sendable, Equatable { let left: Double; let top: Double; let right: Double; let bottom: Double; let confidence: Double }
    let usable: Double?
    let problem: String?
    let part: String?
    let partConfidence: Double?
    let box: Box?
    let sameArea: Double?
    let looksDamaged: Double?
    let tip: String
    let ms: Int
}

/// Something the agent asked the phone to do (bridge agent.ts ClientAction).
nonisolated struct AgentAction: Codable, Sendable, Equatable {
    struct Box: Codable, Sendable, Equatable { let left: Double; let top: Double; let right: Double; let bottom: Double }
    struct Step: Codable, Sendable, Equatable { let say: String; let svg: String }
    let type: String              // scan | closeup | screen | whiteboard
    let subject: String?
    let box: Box?
    let screen: String?
    let title: String?
    let steps: [Step]?
}

nonisolated struct VoiceTurnResult: Codable, Sendable {
    let turn: VoiceTurn
    let actions: [AgentAction]
}
