import message from '@/components/ui/message';
import { downloadFileFromBlob } from '@/utils/file-util';
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import React from 'react';
import { AnswerExportButtons } from './answer-export-buttons';

jest.mock('@/components/ui/message', () => ({
  __esModule: true,
  default: { error: jest.fn() },
}));
jest.mock('@/utils/authorization-util', () => ({
  getAuthorization: () => 'Bearer test',
}));
jest.mock('@/utils/file-util', () => ({ downloadFileFromBlob: jest.fn() }));
jest.mock('../ui/button', () => ({
  Button: (props: Record<string, unknown>) => {
    const attributes = { ...props };
    delete attributes.size;
    delete attributes.variant;
    return jest.requireActual('react').createElement('button', attributes);
  },
}));
jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: { format: string }) => options?.format ?? key,
  }),
}));

const fetchMock = jest.fn();
const originalFetch = globalThis.fetch;

beforeEach(() => {
  Object.assign(globalThis, { React });
  fetchMock.mockReset();
  jest.clearAllMocks();
  globalThis.fetch = fetchMock;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test('exports the chosen answer without disabling another answer', async () => {
  let finish!: (response: unknown) => void;
  fetchMock.mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  render(
    <>
      <AnswerExportButtons
        chatId="chat"
        sessionId="session"
        markdown="# First"
      />
      <AnswerExportButtons
        chatId="chat"
        sessionId="session"
        markdown="# Second"
      />
    </>,
  );

  const [first, second] = screen.getAllByRole('group');
  fireEvent.click(within(second).getByRole('button', { name: 'Excel' }));
  expect(within(first).getByRole('button', { name: 'Excel' })).toBeEnabled();
  expect(within(second).getByRole('button', { name: 'Word' })).toBeDisabled();
  expect(fetchMock).toHaveBeenCalledWith(
    '/api/v1/chats/chat/sessions/session/exports',
    expect.objectContaining({
      body: JSON.stringify({ format: 'xlsx', markdown: '# Second' }),
    }),
  );

  const blob = new Blob(['workbook']);
  finish({
    ok: true,
    headers: { get: () => 'application/octet-stream' },
    blob: async () => blob,
  });
  await waitFor(() =>
    expect(downloadFileFromBlob).toHaveBeenCalledWith(blob, 'report.xlsx'),
  );
});

test('shows an export error and makes the button available again', async () => {
  fetchMock.mockResolvedValue({
    ok: false,
    json: async () => ({ message: 'export_service_busy' }),
  });
  render(
    <AnswerExportButtons chatId="chat" sessionId="session" markdown="Answer" />,
  );
  const button = screen.getByRole('button', { name: 'Word' });
  fireEvent.click(button);
  await waitFor(() =>
    expect(message.error).toHaveBeenCalledWith('chat.exportServiceBusy'),
  );
  expect(button).toBeEnabled();
  expect(downloadFileFromBlob).not.toHaveBeenCalled();
});
