// @vitest-environment jsdom

import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ChartBlock, { parseEchartsOption } from './ChartBlock';

const setOption = vi.fn();
const dispose = vi.fn();
const resize = vi.fn();
const init = vi.fn(() => ({ setOption, dispose, resize }));

vi.mock('echarts', () => ({ init }));

class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

vi.stubGlobal('ResizeObserver', ResizeObserverStub);

const VALID_OPTION = JSON.stringify({
  title: { text: '月度销售' },
  xAxis: { data: ['1月', '2月'] },
  yAxis: {},
  series: [{ type: 'bar', name: '销售额', data: [120, 200] }],
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('parseEchartsOption', () => {
  it('accepts declarative JSON with a series array', () => {
    const option = parseEchartsOption(VALID_OPTION);
    expect(option).not.toBeNull();
    expect(Array.isArray(option?.series)).toBe(true);
  });

  it('rejects malformed JSON, non-objects, and options without series', () => {
    expect(parseEchartsOption('{not json')).toBeNull();
    expect(parseEchartsOption('"just a string"')).toBeNull();
    expect(parseEchartsOption('[1, 2]')).toBeNull();
    expect(parseEchartsOption('{"title": {"text": "no series"}}')).toBeNull();
    expect(parseEchartsOption('')).toBeNull();
  });

  it('rejects oversized payloads', () => {
    const huge = `{"series": [], "pad": "${'x'.repeat(210 * 1024)}"}`;
    expect(parseEchartsOption(huge)).toBeNull();
  });
});

describe('ChartBlock', () => {
  it('renders a chart container with the option title for valid options', async () => {
    render(<ChartBlock code={VALID_OPTION} />);
    expect(screen.getByTestId('md-chart-block')).toBeTruthy();
    expect(screen.getByText('月度销售')).toBeTruthy();
    await waitFor(() => {
      expect(init).toHaveBeenCalledTimes(1);
      expect(setOption).toHaveBeenCalledWith(
        expect.objectContaining({ series: expect.any(Array) }),
      );
    });
  });

  it('falls back to a JSON code block when the option is invalid', () => {
    const { container } = render(<ChartBlock code="{not json" />);
    expect(container.querySelector('.md-chart-block')).toBeNull();
    expect(container.querySelector('[data-language="json"]')).not.toBeNull();
    expect(init).not.toHaveBeenCalled();
  });

  it('falls back to a code block when chart initialization throws', async () => {
    init.mockImplementationOnce(() => {
      throw new Error('no canvas');
    });
    const { container } = render(<ChartBlock code={VALID_OPTION} />);
    await waitFor(() => {
      expect(container.querySelector('.md-chart-block')).toBeNull();
      expect(container.querySelector('[data-language="json"]')).not.toBeNull();
    });
  });
});
