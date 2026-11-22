import message from '@/components/ui/message';
import { Authorization } from '@/constants/authorization';
import { getAuthorization } from '@/utils/authorization-util';
import { downloadFileFromBlob } from '@/utils/file-util';
import { Download, LoaderCircle } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../ui/button';

const formats = [
  { format: 'docx', label: 'Word' },
  { format: 'xlsx', label: 'Excel' },
  { format: 'pptx', label: 'PowerPoint' },
] as const;
type ExportFormat = (typeof formats)[number]['format'];

const errorMessages: Record<string, string> = {
  export_model_unavailable: 'chat.exportModelUnavailable',
  unsupported_export_provider: 'chat.exportProviderUnsupported',
  export_service_unconfigured: 'chat.exportServiceUnconfigured',
  export_service_unreachable: 'chat.exportServiceUnreachable',
  export_service_busy: 'chat.exportServiceBusy',
  export_timeout: 'chat.exportTimeout',
  export_markdown_too_large: 'chat.exportTooLarge',
  empty_export_markdown: 'chat.exportEmptyAnswer',
};

export interface AnswerExportContext {
  chatId: string;
  sessionId: string;
}

export function AnswerExportButtons({
  chatId,
  sessionId,
  markdown,
}: AnswerExportContext & { markdown: string }) {
  const { t } = useTranslation();
  const [exporting, setExporting] = useState<ExportFormat>();
  const activeRequest = useRef<AbortController>();

  useEffect(
    () => () => {
      activeRequest.current?.abort();
      activeRequest.current = undefined;
    },
    [],
  );

  async function startExport(format: ExportFormat) {
    if (activeRequest.current) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    setExporting(format);
    const timeout = window.setTimeout(
      () => controller.abort('timeout'),
      660_000,
    );
    try {
      const response = await fetch(
        `/api/v1/chats/${encodeURIComponent(chatId)}/sessions/${encodeURIComponent(sessionId)}/exports`,
        {
          method: 'POST',
          headers: {
            [Authorization]: getAuthorization(),
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ format, markdown }),
          signal: controller.signal,
        },
      );
      if (!response.ok) {
        const error = await response.json().catch(() => undefined);
        const errorKey =
          typeof error?.message === 'string'
            ? errorMessages[error.message]
            : undefined;
        message.error(t(errorKey ?? 'chat.exportFailed'));
        return;
      }
      if (response.headers.get('Content-Type') !== 'application/octet-stream') {
        message.error(t('chat.exportFailed'));
        return;
      }
      const file = await response.blob();
      if (!controller.signal.aborted) {
        downloadFileFromBlob(file, `report.${format}`);
      }
    } catch {
      if (!controller.signal.aborted) {
        message.error(t('chat.exportFailed'));
      } else if (controller.signal.reason === 'timeout') {
        message.error(t('chat.exportTimeout'));
      }
    } finally {
      window.clearTimeout(timeout);
      if (activeRequest.current === controller) {
        activeRequest.current = undefined;
        setExporting(undefined);
      }
    }
  }

  return (
    <div
      className="flex flex-wrap gap-1"
      role="group"
      aria-label={t('chat.exportAnswer')}
    >
      {formats.map(({ format, label }) => (
        <Button
          key={format}
          type="button"
          size="sm"
          variant="ghost"
          disabled={exporting !== undefined}
          aria-busy={exporting === format}
          aria-label={t('chat.exportAs', { format: label })}
          onClick={() => void startExport(format)}
        >
          {exporting === format ? (
            <LoaderCircle className="size-4 animate-spin" />
          ) : (
            <Download className="size-4" />
          )}
          {label}
        </Button>
      ))}
    </div>
  );
}
