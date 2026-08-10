import * as React from "react"
import * as TabsPrimitive from "@radix-ui/react-tabs"
import { cn } from "@/lib/utils"

export const Tabs = TabsPrimitive.Root

export const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.List
    ref={ref}
    className={cn(
      // max-w-full + overflow-x-auto: on a phone, a tab bar with several
      // labels can be wider than the screen -- scroll the tab bar itself
      // horizontally instead of letting it force the whole page to scroll.
      "inline-flex h-9 max-w-full items-center gap-1 overflow-x-auto rounded-md border border-[var(--color-border)] bg-[var(--color-surface)] p-1",
      className
    )}
    {...props}
  />
))
TabsList.displayName = "TabsList"

export const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Trigger
    ref={ref}
    className={cn(
      "inline-flex shrink-0 items-center whitespace-nowrap rounded-sm px-3 py-1 text-xs font-medium text-[var(--color-text-muted)] transition-colors data-[state=active]:bg-[var(--color-signal-amber-dim)] data-[state=active]:text-[var(--color-signal-amber)]",
      className
    )}
    {...props}
  />
))
TabsTrigger.displayName = "TabsTrigger"

export const TabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content ref={ref} className={cn("mt-4 outline-none", className)} {...props} />
))
TabsContent.displayName = "TabsContent"
