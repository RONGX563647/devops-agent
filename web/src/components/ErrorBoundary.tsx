/** 渲染错误边界：单个面板崩溃时给出可见错误，而不是整页空白。 */
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 保留控制台栈，方便定位是哪个面板炸了
    console.error('页面渲染失败', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="error-boundary" role="alert">
          <h2>页面渲染出错</h2>
          <p className="mono">{this.state.error.message}</p>
          <div className="error-boundary-actions">
            <button onClick={() => this.setState({ error: null })}>重试</button>
            <button onClick={() => window.location.reload()}>刷新页面</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
