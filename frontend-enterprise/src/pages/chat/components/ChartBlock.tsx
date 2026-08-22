import { memo, useEffect, useMemo, useRef, useState } from 'react';

import CodeBlock from '@/components/CodeBlock';

// echarts option 上限：超出按普通 JSON 代码块降级，防止超大 JSON 撑爆渲染。
const MAX_OPTION_CHARS = 200 * 1024;

type EchartsOption = Record<string, unknown> & { series?: unknown };

export function parseEchartsOption(code: string): EchartsOption | null {
  const trimmed = code.trim();
  if (!trimmed || trimmed.length > MAX_OPTION_CHARS) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch {
    return null;
  }
  if (
    parsed !== null
    && typeof parsed === 'object'
    && !Array.isArray(parsed)
    && Array.isArray((parsed as EchartsOption).series)
  ) {
    return parsed as EchartsOption;
  }
  return null;
}

function optionTitle(option: EchartsOption): string {
  const title = option.title;
  if (typeof title === 'string') return title;
  if (title && typeof title === 'object' && !Array.isArray(title)) {
    const text = (title as { text?: unknown }).text;
    if (typeof text === 'string' && text.trim()) return text.trim();
  }
  return '';
}

type InternalChart = {
  setOption: (option: unknown) => void;
  resize: () => void;
  dispose: () => void;
};

type ChartBlockProps = {
  code: string;
};

/**
 * ```echarts 代码块渲染：内容必须是合法 JSON 的 echarts option（声明式，
 * 无函数/无执行面）。解析失败或运行时异常一律降级为原生 JSON 代码块，
 * 内容不丢失。echarts 走动态 import 懒加载，不进主 chunk。
 *
 * 聊天页有轮询/流式重渲染：option 必须 useMemo 稳定引用 + 组件 memo，
 * 否则每次父级重渲染都会销毁重建 echarts 实例，图表反复闪烁。
 */
function ChartBlock({ code }: ChartBlockProps) {
  const option = useMemo(() => parseEchartsOption(code), [code]);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!option || failed) return undefined;
    let disposed = false;
    let chart: InternalChart | null = null;
    let observer: ResizeObserver | null = null;
    import('echarts')
      .then((echarts) => {
        if (disposed || !containerRef.current) return;
        chart = echarts.init(containerRef.current) as unknown as InternalChart;
        chart.setOption(option);
        observer = new ResizeObserver(() => chart?.resize());
        observer.observe(containerRef.current);
      })
      .catch(() => {
        setFailed(true);
      });
    return () => {
      disposed = true;
      observer?.disconnect();
      chart?.dispose();
    };
  }, [option, failed]);

  if (!option || failed) {
    return <CodeBlock code={code} language="json" className="md-code-block" />;
  }
  const title = optionTitle(option);
  return (
    <div className="md-chart-block" data-testid="md-chart-block">
      {title ? <div className="md-chart-caption">{title}</div> : null}
      <div
        ref={containerRef}
        className="md-chart-container"
        style={{ width: '100%', height: 320 }}
        data-testid="md-chart-canvas"
      />
    </div>
  );
}

export default memo(ChartBlock);
