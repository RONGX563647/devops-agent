/** 数字输入：清空 = 用默认（null），不是 0。 */

import type { ModelBound } from '../../../types';
import { toNumber } from './validation';

interface NumProps {
  value: number | null;
  bound?: ModelBound;
  integer?: boolean;
  onChange: (value: number | null) => void;
  disabled?: boolean;
  ariaLabel: string;
}

export function NumInput({ value, bound, integer, onChange, disabled, ariaLabel }: NumProps) {
  return (
    <input
      className="mc-input mono"
      type="number"
      aria-label={ariaLabel}
      value={value ?? ''}
      min={bound?.min}
      max={bound?.max}
      step={bound?.step ?? (integer ? 1 : 0.05)}
      placeholder="默认"
      disabled={disabled}
      onChange={(e) => onChange(toNumber(e.target.value, integer ?? false))}
    />
  );
}
