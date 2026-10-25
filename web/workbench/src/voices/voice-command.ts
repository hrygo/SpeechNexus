import { parse, type FileContracts } from "../file-workflow/speech-file-client";

export type VoiceCommand = Extract<
  FileContracts["JobCreateRequest"],
  { kind: "voice_design" | "voice_clone" }
>;

/** The public generated parser owns wire constraints; this consumer requires explicit scope. */
export function voiceCommand(value: unknown): VoiceCommand {
  const command = parse("JobCreateRequest", structuredClone(value));
  if ((command.kind !== "voice_design" && command.kind !== "voice_clone") || !command.workspace_id)
    throw new Error("voice_command_scope");
  return command;
}

export function checkVoiceReference(command: VoiceCommand, value: unknown): void {
  if (command.kind !== "voice_clone") throw new Error("voice_reference_mode");
  const asset = parse("Asset", value);
  if (
    asset.id !== command.input.reference_audio_asset_id ||
    asset.workspace_id !== command.workspace_id ||
    asset.purpose !== "voice_reference" ||
    asset.status !== "ready" ||
    !asset.media_type.startsWith("audio/")
  )
    throw new Error("voice_reference_unusable");
}
