import SwiftUI

/// Multi-step patch sheet:
///   Step 1 — select hosts (all shown, updates-available pre-checked)
///   Step 2 — sudo password + confirm
///   Step 3 — real-time progress via WebSocket
struct PatchConfirmSheet: View {
    /// All hosts available to patch
    let allHosts: [Host]
    let onDismiss: () -> Void

    @StateObject private var hostService = HostService()
    @StateObject private var wsService = WebSocketService()

    @State private var selectedHostnames: Set<String> = []
    @State private var sudoPassword = ""
    @State private var step: Step = .select
    @State private var errorMessage: String?

    enum Step { case select, confirm, patching }

    // MARK: - Body

    var body: some View {
        NavigationStack {
            Group {
                switch step {
                case .select:   selectStep
                case .confirm:  confirmStep
                case .patching: progressStep
                }
            }
            .padding()
            .background(Theme.bgBlack)
            .navigationTitle(navigationTitle)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(backButtonLabel) {
                        switch step {
                        case .select:          onDismiss()
                        case .confirm:         step = .select
                        case .patching:        onDismiss()   // only reachable when complete
                        }
                    }
                    .foregroundColor(Theme.textSecondary)
                }
            }
        }
        .presentationDetents([.large])
        .interactiveDismissDisabled(false)
        .onAppear { preselectHosts() }
    }

    private var backButtonLabel: String {
        switch step {
        case .select:   return "Cancel"
        case .confirm:  return "Back"
        case .patching: return "Close"
        }
    }

    private var navigationTitle: String {
        switch step {
        case .select:   return "Select Hosts to Patch"
        case .confirm:  return "Confirm Patch"
        case .patching: return "Patching"
        }
    }

    // MARK: - Step 1: Host Selection

    private var selectStep: some View {
        VStack(spacing: 0) {
            // Summary bar
            HStack {
                Text("\(selectedHostnames.count) of \(patchableHosts.count) selected")
                    .font(.subheadline)
                    .foregroundColor(Theme.textSecondary)
                Spacer()
                Button(selectedHostnames.count == patchableHosts.count ? "Deselect All" : "Select All") {
                    if selectedHostnames.count == patchableHosts.count {
                        selectedHostnames = []
                    } else {
                        selectedHostnames = Set(patchableHosts.map(\.hostname))
                    }
                }
                .font(.subheadline)
                .foregroundColor(Theme.cyan)
            }
            .padding(.bottom, 12)

            // Host list
            ScrollView {
                VStack(spacing: 8) {
                    ForEach(allHosts) { host in
                        hostSelectRow(host)
                    }
                }
            }

            // Next button
            Button(action: { step = .confirm }) {
                Text("Next  →")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
                    .padding()
                    .background(selectedHostnames.isEmpty ? Theme.bgCard : Theme.green)
                    .foregroundColor(selectedHostnames.isEmpty ? Theme.textMuted : .white)
                    .cornerRadius(10)
            }
            .disabled(selectedHostnames.isEmpty)
            .padding(.top, 16)
        }
    }

    private func hostSelectRow(_ host: Host) -> some View {
        let isUnreachable = host.status == .unreachable
        let isSelected = selectedHostnames.contains(host.hostname)
        let updates = host.totalUpdates ?? 0

        return Button(action: {
            guard !isUnreachable else { return }
            if isSelected {
                selectedHostnames.remove(host.hostname)
            } else {
                selectedHostnames.insert(host.hostname)
            }
        }) {
            HStack(spacing: 12) {
                // Checkbox
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundColor(isUnreachable ? Theme.textMuted :
                                     isSelected    ? Theme.cyan : Theme.textSecondary)

                // Host info
                VStack(alignment: .leading, spacing: 3) {
                    Text(host.hostname)
                        .font(.subheadline)
                        .fontWeight(.semibold)
                        .foregroundColor(isUnreachable ? Theme.textMuted : Theme.textPrimary)

                    HStack(spacing: 6) {
                        if let ip = host.ipAddress {
                            Text(ip)
                                .font(.caption)
                                .foregroundColor(Theme.textSecondary)
                        }
                        if let os = host.osFamily {
                            Text(os)
                                .font(.caption)
                                .foregroundColor(Theme.textMuted)
                        }
                    }
                }

                Spacer()

                // Right side: update count or status
                VStack(alignment: .trailing, spacing: 4) {
                    StatusBadge(status: host.status)
                    if updates > 0 {
                        Text("\(updates) pkg\(updates == 1 ? "" : "s")")
                            .font(.caption2)
                            .foregroundColor(Theme.amber)
                    } else if isUnreachable {
                        Text("Unreachable")
                            .font(.caption2)
                            .foregroundColor(Theme.red)
                    }
                }
            }
            .padding(12)
            .background(isSelected ? Theme.bgCardHover : Theme.bgCard)
            .cornerRadius(8)
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(isSelected ? Theme.cyan.opacity(0.5) : Theme.border, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .disabled(isUnreachable)
        .opacity(isUnreachable ? 0.5 : 1.0)
    }

    // MARK: - Step 2: Confirm + Sudo Password

    private var confirmStep: some View {
        VStack(spacing: 16) {
            // Selected host list (read-only summary)
            VStack(alignment: .leading, spacing: 6) {
                Text("Patching \(selectedHostnames.count) host\(selectedHostnames.count == 1 ? "" : "s"):")
                    .font(.subheadline)
                    .foregroundColor(Theme.textSecondary)

                ForEach(Array(selectedHostnames.sorted()), id: \.self) { name in
                    Label(name, systemImage: "desktopcomputer")
                        .font(.subheadline)
                        .foregroundColor(Theme.textPrimary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
            .background(Theme.bgCard)
            .cornerRadius(10)
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(Theme.border, lineWidth: 1))

            // Sudo password
            VStack(alignment: .leading, spacing: 6) {
                Text("Sudo Password")
                    .font(.caption)
                    .foregroundColor(Theme.textSecondary)
                SecureField("Leave blank if not required", text: $sudoPassword)
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
            }

            if let error = errorMessage {
                Text(error)
                    .font(.caption)
                    .foregroundColor(Theme.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            Spacer()

            Button(action: startPatch) {
                Label("Start Patching", systemImage: "arrow.down.circle.fill")
                    .fontWeight(.semibold)
                    .frame(maxWidth: .infinity)
                    .padding()
                    .background(Theme.green)
                    .foregroundColor(.white)
                    .cornerRadius(10)
            }
        }
    }

    // MARK: - Step 3: Progress

    private var progressStep: some View {
        VStack(spacing: 12) {
            // Status header
            HStack {
                if wsService.patchComplete {
                    Image(systemName: "checkmark.circle.fill").foregroundColor(Theme.green)
                    Text("Patching Complete").foregroundColor(Theme.green)
                } else {
                    ProgressView()
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Patching in progress...").foregroundColor(Theme.amber)
                        Text("Safe to close — patch continues on server")
                            .font(.caption2)
                            .foregroundColor(Theme.textMuted)
                    }
                }
                Spacer()
            }
            .font(.headline)

            // Live terminal output
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(wsService.messages.enumerated()), id: \.offset) { idx, msg in
                            Text(msg)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundColor(messageColor(msg))
                                .id(idx)
                        }
                    }
                    .padding(4)
                }
                .onChange(of: wsService.messages.count) { _, count in
                    if count > 0 { proxy.scrollTo(count - 1, anchor: .bottom) }
                }
            }
            .frame(maxHeight: .infinity)
            .padding(8)
            .background(Theme.bgCardInner)
            .cornerRadius(8)

            if wsService.patchComplete {
                Button("Done") { onDismiss() }
                    .buttonStyle(.borderedProminent)
                    .tint(Theme.cyan)
            }
        }
    }

    // MARK: - Helpers

    /// Hosts that can actually be patched (not unreachable)
    private var patchableHosts: [Host] {
        allHosts.filter { $0.status != .unreachable }
    }

    private func preselectHosts() {
        // Pre-select hosts that have updates available
        selectedHostnames = Set(
            allHosts
                .filter { $0.status == .updatesAvailable }
                .map(\.hostname)
        )
    }

    private func startPatch() {
        errorMessage = nil
        step = .patching
        wsService.connect()

        Task {
            do {
                try await hostService.patchHosts(
                    hostnames: Array(selectedHostnames),
                    becomePassword: sudoPassword.isEmpty ? nil : sudoPassword
                )
            } catch {
                errorMessage = error.localizedDescription
                step = .confirm
                wsService.disconnect()
            }
        }
    }

    /// Color-code terminal output lines
    private func messageColor(_ msg: String) -> Color {
        let lower = msg.lowercased()
        if lower.contains("error") || lower.contains("failed") || lower.contains("fatal") {
            return Theme.red
        }
        if lower.contains("ok") || lower.contains("changed") || lower.contains("success") {
            return Theme.green
        }
        if lower.contains("skipping") || lower.contains("warning") {
            return Theme.amber
        }
        return Theme.textSecondary
    }
}
