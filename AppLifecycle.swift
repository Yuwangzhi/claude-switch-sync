import AppKit
import Foundation

let bundle = "com.anthropic.claudefordesktop"
let command = CommandLine.arguments.dropFirst().first ?? "status"
let running = NSRunningApplication.runningApplications(withBundleIdentifier: bundle)
switch command {
case "quit":
    // A normal quit request: never SIGKILL/forceTerminate. If the app declines,
    // the synchronizer keeps the handoff pending and does not touch its index.
    let accepted = running.map { $0.terminate() }.allSatisfy { $0 }
    exit(accepted ? 0 : 2)
case "open":
    if !running.isEmpty { exit(0) }
    let config = NSWorkspace.OpenConfiguration()
    config.activates = false
    var finished = false
    var succeeded = false
    NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: "/Applications/Claude.app"), configuration: config) { app, error in
        succeeded = app != nil && error == nil
        finished = true
    }
    let deadline = Date().addingTimeInterval(10)
    while !finished && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    exit(succeeded ? 0 : 3)
default:
    print(running.map { String($0.processIdentifier) }.joined(separator: ","))
}
