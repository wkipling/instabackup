// Decompile every function whose name matches a regex.
// Usage: -postScript DecompileNamed.java <outputDir> <regex>
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.program.model.listing.Function;
import java.io.File;
import java.io.PrintWriter;
import java.util.regex.Pattern;

public class DecompileNamed extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File outDir = new File(args[0]);
        outDir.mkdirs();
        Pattern pattern = Pattern.compile(args[1]);

        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);

        for (Function f : currentProgram.getFunctionManager().getFunctions(true)) {
            if (!pattern.matcher(f.getName()).find()) {
                continue;
            }
            DecompileResults res = di.decompileFunction(f, 120, monitor);
            if (!res.decompileCompleted()) {
                println("FAIL " + f.getName() + " @ " + f.getEntryPoint());
                continue;
            }
            String c = res.getDecompiledFunction().getC();
            String safe = f.getName().replaceAll("[^A-Za-z0-9_.-]", "_");
            File out = new File(outDir, safe + ".c");
            try (PrintWriter w = new PrintWriter(out)) {
                w.println("// " + f.getName() + " @ " + f.getEntryPoint());
                w.println(c);
            }
            println("WROTE " + out);
        }
        di.dispose();
    }
}
